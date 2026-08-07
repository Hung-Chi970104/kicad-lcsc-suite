# Qt migration plan

Moving kicad-lcsc-suite from an in-process wxPython plugin to an
out-of-process PySide6 application driven over KiCad 10's IPC API.

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (how the current system fits
together) and [DEVELOPMENT.md](DEVELOPMENT.md) (setup and tests).

---

## 1. Why

Three reasons, in order of weight.

1. **Cross-platform inconsistency, confirmed on Windows.** wxWidgets wraps
   *native* controls — `wxDataViewCtrl` is `NSOutlineView` on macOS and a
   different control on Windows, with different row heights, font metrics,
   padding and overflow behaviour. A layout tuned on one platform is wrong on
   the other, and no amount of better wx code fixes it. Qt draws its own
   widgets; with the Fusion style forced, both platforms render the same.
2. **Verifiability.** Qt renders fully offscreen (`QT_QPA_PLATFORM=offscreen`)
   and `QWidget.grab()` produces a pixel-exact PNG with no display, no window
   manager and no screen-recording permission. Because Fusion renders
   identically across platforms, **a screenshot taken on macOS is real
   evidence about Windows** — which is what wx can never provide.
3. **Python 3.9.** KiCad 10.0.3 still bundles Python 3.9.13, so targeting
   KiCad 10 does *not* lift the 3.9 constraint. Only leaving KiCad's
   interpreter does. PySide6 requires ≥3.10, so Qt forces the move — and the
   move is the thing that pays.

### Non-goals

- No KiCad 9 or earlier support. KiCad 10 only.
- No rewrite of the network layer. See §4.
- No feature changes beyond the removal below. This is a port; behaviour and
  layout carry over. New features come after parity.

### Out of scope — Gerber and drill generation (decided 2026-08-07)

**Gerber and Excellon drill output is dropped, not ported.** Another plugin
the user already trusts handles fabrication output. This removes the only
phase whose feasibility was never proven (`PLOT_CONTROLLER` → `kicad-cli`),
and with it the plan's highest risk.

Deleted outright: the `Generate` button and its `Auto` dropdown,
`fabrication.py`'s plot path, `kicad_drc.py`, `generate_hooks.py`, `HOOKS.md`,
the pre/post generation hooks, and every Gerber-plotting setting (tented vias,
fill zones, force DRC, plot values, plot references, subtract soldermask,
order-number placeholder check).

**Kept: the BOM and CPL writers.** They are the LCSC-specific output nothing
else can produce — the other plugin does not know this project's LCSC
assignments — and the CPL is what consumes the rotation/offset corrections, so
dropping it would cascade into deleting the whole Corrections subsystem
(`corrections.py`, its dialog, and its database). They are pure logic that
needed no porting anyway; only the plot path did.

---

## 2. Target architecture

```text
KiCad 10 (pcbnew)                         LCSC Suite (separate process)
│                                          │
├─ reads ~/Documents/KiCad/10.0/plugins/   │
│    lcsc_suite/plugin.json                │
│      └─ toolbar button "LCSC Suite"      │
│                                          │
├─ on click: exec run.sh ────────────────► launcher clears PYTHONHOME,
│    env: KICAD_API_SOCKET                 │  runs venv python -m lcsc_suite
│         KICAD_API_TOKEN                  │
│                                          ├─ PySide6 UI (Fusion style)
└─◄──── IPC API (kipy) ───────────────────┤─ lcsc/ network layer (unchanged)
     board, footprints, fields, commits    ├─ SQLite stores (unchanged)
                                           └─ BOM / CPL writers (pure logic)
```

The manifest declares the button. `runtime.type: "exec"` is the key: KiCad
launches **any executable**, so the app brings its own Python and KiCad does
not care. That also means swapping the venv for a frozen binary later is a
drop-in change to one shell script.

```json
{
  "identifier": "com.lcscsuite.plugin",
  "name": "LCSC Suite",
  "description": "LCSC/JLCPCB part assignment, library import and fabrication output",
  "runtime": { "type": "exec" },
  "actions": [
    {
      "identifier": "open-suite",
      "name": "LCSC Suite",
      "description": "Open the LCSC Suite window",
      "show-button": true,
      "scopes": ["pcb"],
      "entrypoint": "run.sh",
      "icons-light": ["icons/lcsc-suite.png"],
      "icons-dark": ["icons/lcsc-suite-dark.png"]
    }
  ]
}
```

### Verified in the spike

All of the following was proven end to end against KiCad 10.0.3 before this
plan was written, not assumed:

| Capability | Status |
|---|---|
| Toolbar button appears in pcbnew | ✅ screenshotted |
| Clicking it launches our own executable | ✅ `runtime.type: exec` |
| KiCad passes `KICAD_API_SOCKET` + `KICAD_API_TOKEN` | ✅ |
| App runs Python 3.14 + PySide6, not KiCad's 3.9 | ✅ |
| Read board name, footprints, refs, values, footprint ids | ✅ |
| Read custom `LCSC` field **and its hidden flag** | ✅ |
| Update custom `LCSC` field | ✅ verified by re-read |
| Create `LCSC` field where none existed | ✅ verified by re-read |
| Exclude-from-BOM / exclude-from-POS / DNP attributes | ✅ plain booleans |
| Qt offscreen render → self-screenshot | ✅ no display needed |
| Footprint position + rotation (for CPL) | ⚠️ exposed on the API, **not exercised** |

### Four traps — read before writing code

Traps 1–3 were found in the spike. **Trap 4 was found in Phase 3, the first time
a write crossed a real socket**, and it had defeated the original design of the
verification itself. Everything before that had been proven against a fixture
that was more permissive than the API.

1. **KiCad poisons the environment.** It hands its own `PYTHONHOME` down to
   `exec` plugins, which kills a venv Python instantly with
   `ModuleNotFoundError: No module named 'encodings'`. The launcher **must**
   `unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE PYTHONSTARTUP` before
   exec'ing. This is the single most confusing first-day failure available.

2. **The API silently ignores writes to the wrong object.** Calling
   `board.update_items(field)` on a field object returns success and changes
   nothing. You must call `board.update_items(footprint)` on the **parent
   footprint**. Both spellings raise no exception. Every write path needs a
   read-back assertion in tests; do not trust return values.

3. **Custom fields are not on the footprint.** They live in
   `footprint.definition.items` (surfaced via `.texts_and_fields`), so
   creating one goes through `footprint.definition.add_item(field)`, then
   `update_items(footprint)`. Clone an existing `Field` and
   `proto.ClearField("id")` rather than constructing one.

4. **An open commit is invisible to a read, so you cannot verify before you
   commit.** `update_items` applies immediately when no commit is open, but
   between `begin_commit()` and `push_commit()` the board keeps answering
   `get_footprints()` from the *committed* state. The obvious ordering —
   mutate, verify, commit only if the board agrees — therefore fails every
   time, and fails **looking exactly like trap 2**: a clean return value and an
   unchanged board.

   `kicad_bridge._Board.apply` snapshots first, pushes, *then* verifies, and on
   a mismatch puts the snapshot back in a second commit. `drop_commit` does roll
   back correctly — it is simply unreachable as a response to a verification
   that cannot have run yet. The price is that a failed write leaves two entries
   in KiCad's undo history rather than none; the board itself ends up unchanged
   either way.

   `FixtureBoard` reproduces this as well as trap 2 — staged in `_pending`,
   visible only on `_push`. It did not, before Phase 3, and that is precisely
   why the bug survived two phases of green tests.

---

## 3. What the code splits into

~15,400 lines of Python outside `lib/`. The split is favourable: the expensive,
hard-won logic is already toolkit-free.

### Rewritten — the UI (~10,100 lines)

| File | Lines | Becomes |
|---|---|---|
| `mainwindow.py` | 2851 | `ui/main_window.py` + extracted controllers |
| `lcsc/explorer.py` | 2918 | `ui/explorer/` (search, results, facets, detail) |
| `settings.py` | 1060 | `ui/settings_dialog.py` |
| `corrections.py` | 676 | `ui/corrections_dialog.py` (logic half stays) |
| `datamodel.py` | 551 | `ui/models/part_table.py` (`QAbstractTableModel`) |
| `dataview_highlight.py` | 358 | delegate on the table view |
| `partdetails.py` | 335 | `ui/part_details_dialog.py` |
| `bom_widget.py` | 272 | `ui/bom_estimator.py` |
| `partmapper.py` | 258 | `ui/mappings_dialog.py` |
| `lcsc/photoviewer.py` | 244 | `ui/photo_viewer.py` |
| `lcsc/previewpanel.py` | 228 | `ui/explorer/preview.py` |
| `lcsc/facetfilter.py` | 211 | `ui/explorer/facets.py` |
| `lcsc/theme.py` | 134 | `ui/theme.py` (Qt palette + stylesheet) |

### Ported nearly unchanged — the logic (~5,300 lines)

Already free of wx, and in most cases free of pcbnew:

| File | Lines | Change needed |
|---|---|---|
| `lcsc/api.py` | 1115 | **none** — zero wx references |
| `library.py` | 925 | none (SQLite only) |
| `corrections.py` (logic half) | — | none (SQLite only) |
| `schematicexport.py` | 466 | none — parses `.kicad_sch` directly |
| `store.py` | 454 | none (SQLite only) |
| `fabrication.py` | 511 | **strip the plot path**; keep BOM/CPL writers |
| `lcsc/importer.py` | 399 | none |
| `lcsc/details.py` | 278 | strip 2 wx references |
| `schematicimport.py` | 209 | none |
| `derive_params.py` | 171 | none |
| `footprint_helpers.py` | 108 | **rewrite** → kipy field access |
| `bom_estimation/`, `enrichment/`, `dblib/`, `common/` | ~1200 | none |

The five SQLite databases (parts, part cache, project, corrections, mappings)
and all their schemas carry over untouched. So does the `db_build/` GitHub
Action pipeline.

### Deleted

`standalone_impl.py` (wx/pcbnew stubs — obsolete once out-of-process),
`plugin.py`, `helpers.py` wx helpers, `events.py` (wx events → Qt signals),
`search_escape.py`, `partselector_columns.py` and `PartSelectorDataModel`
(already dead code per ARCHITECTURE.md §4).

### Two things that make this cheaper than it looks

- **No i18n to port.** There is no `gettext`, no `wx.Locale` and no
  translation catalogue anywhere in the tree. (`common/translate.py` is
  database *format* translation, not language translation.) If localisation is
  ever wanted, Qt's `tr()` is a clean greenfield addition.
- **No test imports wx.** Nothing under `tests/` or `common/` touches the
  toolkit, so the entire existing suite ports unchanged and keeps working
  through every phase. It is the safety net for the logic half.

### Smaller items that still need a decision

- **`settings.json` location.** Today it is written to `PLUGIN_PATH` — i.e.
  into the checkout, where it is a *tracked file that the running plugin
  mutates*. It is dirty in the working tree right now for exactly that reason.
  Out-of-process this must move to a per-user config directory
  (`QStandardPaths.AppConfigLocation`), because a frozen binary's install
  directory may be read-only. Needs a one-time import of the old file, and the
  old path should be gitignored regardless of this migration.
- **Icon set.** 55 PNGs in `icons/`. Qt renders disabled states natively, so
  `settings.py`'s hand-rolled "draw a red X over the bitmap" disabled-icon
  code (`create_disabled_bitmap`) is deleted rather than ported. The icons
  themselves need `@2x` variants for high-DPI, which the manifest already
  supports via multiple paths per `icons-light`/`icons-dark` entry.
- **`selfcheck.py`** imports pcbnew for diagnostics. Drop it — most of what it
  checked was fabrication readiness, which is now out of scope.

---

## 4. The network layer does not get rewritten

This is load-bearing and deliberate. [`lcsc/api.py`](../lcsc/api.py) is 1115
lines with **zero** wx references. It encodes domain knowledge that took the
bulk of the project's effort and is not re-derivable from a spec:

- **Three disagreeing sources.** JLC assembly stock, LCSC retail stock and the
  EasyEDA `szlcsc` fallback are different inventories that routinely differ by
  orders of magnitude. `StockReport` carries both plus derived warnings
  (`UNAVAILABLE`, `Assembly-blocked`, `Assembly-only`, divergence factor).
  **Never collapse them into one "stock" number** — that is the bug this fork
  exists to fix.
- **Reachability rules.** `wmsc.lcsc.com` bulk 403s anonymous clients; LCSC
  403s *whole networks*, taking the API, product pages and image CDN with it.
  Hence: no user-visible feature may depend on `lcsc.com` alone, retail stock
  falls back to EasyEDA, and photos come from JLC's file service.
- **`_HostBreaker`.** Three consecutive hard failures open a host for ten
  minutes. Without it a 120-row fill is 120 round trips to learn the same
  fact, and on EasyEDA's throttle that converts a soft limit into a ban.
- **`None` ≠ `0`.** A source answering nothing renders `?`; a part with no
  stock renders `0`. Conflating them shows in-stock parts as unavailable.

**Rule for the migration: `lcsc/api.py` is copied, not edited.** If a UI need
seems to require an API change, change the UI. The only permitted edit is
dropping Python 3.9 compatibility shims once nothing on 3.9 imports it.

Threading changes shape but not substance: today's `wx.CallAfter` +
worker-thread pattern becomes `QThreadPool` workers emitting Qt signals. The
API layer is already defensive about threads and teardown, and stays
synchronous and unaware of the UI.

---

## 5. UI inventory — what must be reproduced

Captured from the running plugin, not from memory. Screenshots taken via
offscreen wx capture; these are the reference for visual parity.

### 5.1 Main window — 1300×772

**Upper-left toolbar:** in the wx version this is `Generate` plus an `Auto`
dropdown. **Both are dropped** (§1). Replace with a single `Export BOM / CPL`
button; the left toolbar is otherwise empty.

An `Undo` button precedes it, which the wx plugin did not have. See the Undo
entry in §10 — it is not cosmetic, and KiCad's own history is not a substitute.

**Upper-right toolbar**, left to right: `From schematic`, `To schematic`,
`Corrections`, `Mappings`, `LCSC Explorer`, `Import libs`, `Offline DB`,
`Settings`. Each is an icon above a text label.

> The two schematic buttons must stay **two explicit buttons**, each warning
> about what it overwrites. Board↔schematic sync is never automatic.

**Second row:** `Boards:` spin control (default 5) · `Force Standard`
checkbox · `Help` button.

**Status line:** `BOM Estimate (5 boards): no assigned BOM parts`.

**Part list table**, columns in order:

| # | Column | Notes |
|---|---|---|
| 0 | `Ref` | red when unassigned |
| 1 | `Value (Name)` | |
| 2 | `Footprint` | |
| 3 | `LCSC Params` | derived via `derive_params.py` |
| 4 | `LCSC` | assigned number |
| 5 | `Type` | Basic / Preferred / Extended |
| 6 | `JLC Stock` | `?` when unknown, `0` when out of stock |
| 7 | `BOM` | ✓ / blank |
| 8 | `POS` | ✓ / blank |

Row colouring: unassigned rows use `unassigned_colour`, standard-mode trigger
parts use `standard_trigger_colour` (both toggleable in Settings). Search
match highlighting is a separate toggle.

**Right vertical toolbar**, top to bottom: `Assign LCSC number`,
`Remove LCSC number`, `Auto-select alike`, `Toggle BOM POS`, `Toggle BOM`,
`Toggle POS`, `Part details`, `Hide excluded BOM`, `Hide excluded POS`,
`Save mappings`. Buttons enable/disable on selection.

**Bottom pane:** scrolling log with timestamp, level, function and message.

**Row context menu:** Copy LCSC · Paste LCSC · Add correction by reference ·
by package · by name · Find mapping · Add mapping.

**Single-instance rule:** clicking the toolbar button must raise an existing
window rather than open a second one — two instances share a project database
and a board and would overwrite each other. Out-of-process this becomes a
lock file or single-instance socket instead of `wx.GetTopLevelWindows()`.

### 5.2 LCSC Explorer — 1470×831

**Search row:** `Find parts` label, keyword field, `Search`, `Refresh data`
(clears caches and re-arms every host breaker — also the "I fixed my
connection" button).

**Filter row:** `Inventory:` (JLC assembly / LCSC retail / Both inventories) ·
`Library:` (All / Basic only / Extended only) · `Sort:` (Best match / JLC
assembly stock high first / LCSC retail stock high first) · `In JLC stock`
checkbox · `Filters ▴/▾` toggle · `Details:` (Side panel / Inline below).

**Parametric filter panel** (collapsible): one labelled combo per attribute
discovered in the result set, `Clear filters`, and the explanatory line
`N attributes available; tick any number of values per attribute. Counts are
over the fetched result set.`

**Result count line:** `18326 parts match '10nF 0402'; showing the first 100.
Narrow the keyword to see the rest.`

**Results grid**, columns: thumbnail (108 px rows) · `Part` (MPN over
`C1524 · Extended`) · `Description` (title over grey category subtitle) ·
`Manufacturer / Package` · `JLC assembly` (green, thousands-separated) ·
`LCSC retail` · `Unit price` (over `Min order N`) · trailing spacer.

The `JLC assembly` / `LCSC retail` columns show and hide according to the
Inventory selector — one collapses to width 0 rather than being removed.

**Footer:** state warning (`No footprint selected — Import still works;
select footprints in the main window to enable Assign.`) · `Library folder:`
path + `Browse…` + `Overwrite existing` checkbox.

**Buttons:** `Import and assign` · `Assign number only` ·
`Import library assets` · `Open LCSC` · `Open JLCPCB` · `Open datasheet` ·
`Close`.

**Behaviours to preserve:** double-click a row assigns · clicking a thumbnail
opens the photo viewer (retargetable to another part while open, and must
survive a late thumbnail callback after close) · detail pane works both as a
side panel and inline below · thumbnails come from JLC's file service, one id
per search row, so a full grid costs no extra JSON lookups.

### 5.3 Settings

The wx dialog is a two-column grid of icon + checkbox. **Most of it is
Gerber-plotting settings and is dropped** (§1): tented vias, fill zones, force
DRC, plot values, plot references, subtract soldermask, order-number
placeholder, and the whole `Generation hooks` group.

**What remains** — a much smaller dialog:

- `LCSC numbers from database have priority`
- `Add parts without LCSC number to BOM/POS`
- `Highlight search matches` (under a `Match highlighting` label)
- `Highlight standard-mode trigger parts`
- `Show BOM cost estimator`
- `Parts Library:` dropdown — `Current Parts (Exclude Obsolete)` and the other
  variants from `dblib/__init__.py`
- `Database directory:` field + `Browse`
- `Help`

Each checkbox has a paired inverted label (e.g. `Don't highlight search
matches`) that swaps with state. Since this dialog is now small, lay it out as
a single column rather than reproducing the two-column grid.

`create_disabled_bitmap` (the hand-drawn red X) is deleted — Qt renders
disabled icons natively.

### 5.4 Corrections Manager

Table: `Regex` · `Pattern` · `Rotation` · `Offset X` · `Offset Y`.
Buttons: `Add / Edit` · `Update` · `Delete` · `Import` · `Export` · `Save` ·
`Use global corrections` toggle (switches between the global corrections DB
and the project DB, with a confirmation).

### 5.5 Mappings Manager — 800×772

Table: `Footprint` · `Value` · `LCSC Part`. Buttons: `Delete` (disabled with
no selection) · `Import` · `Export`.

### 5.6 Part Details

Fields: `Designator` · `Component Code` · `Full Name` · `Brand` ·
`Description` · `Model` · `Assembly Process` · `Minimal Quantity` ·
`Minimum price` · Basic/Extended.
Price ladders rendered as `JLC Price for {start}-{end}` and
`LCSC Price for >{start}` rows.
Buttons: `Open LCSC page` · `Open Datasheet` · `Download Datasheet`.
Shows `Loading part details…` while fetching.

### 5.7 Photo viewer

Title `{lcsc} — product photo`. Opens from an Explorer thumbnail, can be
retargeted to a different part while open, and must not crash when a
thumbnail decode lands after it closes.

### 5.8 BOM cost estimator

Inline widget under the main toolbar; board-count driven; hideable from
Settings. Controller logic already lives in `bom_estimation/` and is tested.

---

## 6. Phases

Each phase ends with the app runnable and screenshots checked in. The wx
plugin stays installed and working throughout — it is a separate entry point,
so the two never collide.

### Phase 0 — Skeleton and harness (no features)

- `lcsc_suite/` package; `plugin.json`; `run.sh` with the `PYTHONHOME` unset.
- `install.sh` / `install.ps1` bootstrap a venv and pip-install
  `PySide6` + `kicad-python`.
- `kicad_bridge.py`: connect, get board, read/write footprint fields —
  wrapping the three spike traps so no caller can hit them. Every write helper
  read-backs and raises on mismatch.
- **`scripts/qt_probe.py`**: build any screen offscreen, `grab()` it to
  `docs/screens/<name>.png`, dump widget geometry. This is the acceptance tool
  for every later phase.
- Fusion style forced app-wide; light and dark palettes in `ui/theme.py`.
- CI job that renders every screen offscreen and fails on an exception.

**Done when:** the toolbar button opens an empty Qt window titled
`LCSC Suite`, and `qt_probe.py` screenshots it headlessly.

### Phase 1 — Main window shell

Toolbars (both), board-count row, log pane, status line, single-instance lock,
window geometry persistence. Part table present but empty.

### Phase 2 — Part table

`QAbstractTableModel` over `store.py`. All nine columns, row colouring,
sorting, multi-select, the context menu, and the BOM/POS toggles. Footprint
reads go through `kicad_bridge`.

**Done when:** the table matches `docs/screens/01_mainwindow.png` column for
column, and toggling BOM/POS round-trips to the board and back.

### Phase 3 — Assignment path

`Assign LCSC number`, `Remove LCSC number`, `Auto-select alike`,
`Save mappings`, plus field create/update through the bridge. This is the
phase where trap #2 bites, so read-back assertions are mandatory.

### Phase 4 — LCSC Explorer

The biggest single piece (2918 lines today). Sub-order: search + results grid
→ thumbnails → inventory/sort/stock filters → parametric facets → detail pane
(side and inline) → photo viewer → import/assign buttons.

`lcsc/api.py` is imported as-is. Worker threads become `QThreadPool` +
signals; the debounce, cooldown and per-part caps from ARCHITECTURE.md §4
carry over unchanged.

### Phase 5 — Remaining dialogs

Settings, Corrections, Mappings, Part Details, BOM estimator.

### Phase 6 — BOM / CPL export

Not fabrication — see §1. Only the two files that carry LCSC data:

- **BOM**: designator grouping, LCSC number, exclusions from `store.py`.
- **CPL**: position data with rotation/offset corrections applied.

Both writers already exist in `fabrication.py` as pure logic and are covered by
`tests/test_fabrication_corrections.py`. Work here is a UI entry point plus
reading footprint positions through the bridge instead of pcbnew. Verify by
byte-comparing output against the wx plugin on the same board.

### Phase 7 — Schematic sync

`schematicexport.py` and `schematicimport.py` already parse `.kicad_sch`
directly, so they port unchanged. Only the two buttons and their warning
dialogs are rebuilt.

### Phase 8 — Parity gate, then cutover

- Side-by-side screenshot review of every screen against `docs/screens/`.
- Same board through both versions: identical BOM and CPL.
- Windows verification (see §7).
- Only then remove the wx plugin and the `install.sh` symlink path.

---

## 7. Verification

**The rule that motivated this migration: a claim about the UI is not made
until a screenshot has been looked at.**

- `scripts/qt_probe.py <screen>` renders offscreen and writes a PNG plus a
  geometry dump. No display, no permissions, works in CI.
- `docs/screens/*.png` are committed. A UI change that alters a screen must
  update its screenshot in the same commit, so the diff shows the visual
  change.
- Fusion style + explicit font sizes mean these screenshots are
  cross-platform evidence. That is the whole point.
- `pytest-qt` for interaction tests (`QTest.mouseClick`, signal spies) —
  wx had no automated UI coverage at all.
- Every `kicad_bridge` write test asserts by **re-reading the board**, never
  on a return value.
- One real Windows pass per phase, not per commit: run `qt_probe.py` on
  Windows and diff the PNGs against the macOS set. Non-zero diff is a bug.

Correction to [AGENTS.md](../AGENTS.md): `screencapture` **does** work on the
current dev machine, and wx windows can also be captured offscreen via
`wx.WindowDC` → `wx.Bitmap`. The old claim that UI could only be checked by
geometry dumps is wrong and should be removed once this plan lands.

---

## 8. Open decisions and risks

### Decision — distribution

Plan assumes **venv now, frozen binary later**: `install.sh` / `install.ps1`
bootstrap a venv during development; a PyInstaller freeze is added before any
public release. Because `runtime.type` is `exec`, that swap touches only
`run.sh`.

**This breaks rule #3 in [AGENTS.md](../AGENTS.md)** ("no new runtime
dependencies — must run on a bare KiCad install"). That rule has to be
rewritten as part of Phase 0. Flagging explicitly because it is a product
decision, not a technical one: users gain a one-time setup step in exchange
for a UI that behaves the same on both platforms.

### Open question — PCM packaging

`PCM/` already exists (`create_pcm_archive.sh`, `metadata.template.json`,
still carrying upstream's `com.github.bouni.kicad-jlcpcb-tools` identifier), so
there is a Plugin and Content Manager distribution path today.

**Unanswered: whether PCM can ship an `exec`-runtime plugin that carries its
own Python runtime.** PCM was designed around in-process Python plugins. If it
cannot, distribution becomes a manual install or a platform installer, which
changes the answer to the venv-vs-frozen question above.

This needs resolving before Phase 8 but blocks nothing before it — settle it
while Phases 1–5 are in progress. Do not delete the wx plugin until it is
answered, because PCM is currently the only supported install path for users
who do not clone the repo.

### Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Silent no-op writes (trap #2) | **high** | Read-back assertions in every bridge write; no exceptions |
| IPC API changes between KiCad 10.x releases | medium | Pin `kicad-python`; a smoke test that fails loudly on API drift |
| Explorer is 2918 lines of subtle threading | medium | Phase 4 sub-ordered; port `api.py` untouched so only UI threading is new |
| Upstream merges get harder | medium | Already true; `UPSTREAM.txt` fork is diverging regardless. Accept and record it |
| PCM cannot ship a bundled runtime | medium | Resolve during Phases 1–5; keep the wx plugin installable until it is |
| Scope creep during rewrite | medium | Parity first. No new features before Phase 8 |

### Licensing

**PySide6 (LGPLv3), not PyQt6 (GPLv3).** PyQt would force this MIT project to
become GPL. Same underlying toolkit. The existing AGPL boundary around
`lib/easyeda2kicad/` is unaffected.

---

## 9. Environment changes already made

From the spike, still in place on the dev machine:

- **KiCad's API server enabled** in
  `~/Library/Preferences/kicad/10.0/kicad_common.json` (`api.enable_server:
  true`). It was off by default; the plugin requires it. **`install.sh` must
  check this and tell the user how to enable it** — Preferences → Plugins →
  Enable KiCad API. A clear error beats a silent failure to connect.
- A throwaway spike plugin at `~/Documents/KiCad/10.0/plugins/lcsc_spike`
  (the magenta `Q` button). Delete that folder before Phase 0.

---

## 10. Progress

One entry per phase, kept current as the work lands. Together with the
committed screenshots in `docs/screens/`, this is how the migration is resumed
after a break.

Screens are named `docs/screens/<screen>.png` (plus `<screen>-dark.png`), not
the `01_`-prefixed form §6 guessed at — `scripts/qt_probe.py --list` is the
authoritative list.

### Phase 0 — Skeleton and harness ✅

- `lcsc_suite/` package with `kicad_bridge.py`, `config.py`, `app.py`,
  `ui/theme.py`, `ui/main_window.py`.
- `kicad_plugin/` holds the manifest and the launcher; `install.sh --app`
  bootstraps the venv, links the plugin and **checks the API server setting**.
- `scripts/qt_probe.py` renders offscreen to `docs/screens/`; CI job
  `.github/workflows/qt-screens.yml` renders every screen in both appearances
  and fails on an exception or a missing PNG.
- Screens: `mainwindow.png`, `mainwindow-dark.png`.

Deviations and deferrals, with reasons:

- **`library.py` and `unzip_parts.py` were not "unchanged" after all.** §3
  claimed `library.py` needs no change, but it imports `wx` and posts progress
  with `wx.PostEvent`, so the Qt interpreter could not import it at all. Fixed
  at the seam rather than by stubbing wx: `events.post()` now dispatches to a
  `post_event` sink when the destination has one and to `wx.PostEvent`
  otherwise, and the two modules call it. The wx plugin's dialog has no such
  attribute, so its behaviour is byte-identical. Same reason moved
  `natural_sort_collation` / `dict_factory` out of `helpers.py` (which imports
  wx) into `sqlite_helpers.py`, re-exported for existing importers.
- **`schematicexport.py` imported `pcbnew` for one call.** `GetBuildVersion()`
  now has an optional import and `load_schematic(paths, version=None)` accepts
  the version the app already gets over IPC.
- **Live IPC write verification is deferred to Phase 3.** KiCad shut down
  during the Phase 0 spike-checking session, so the write path is currently
  proven against `FixtureBoard`, which reproduces trap 2 exactly
  (`honour_footprint_writes=False`) — see `tests/test_kicad_bridge.py`. A live
  pass against a *copy* of a real board belongs in Phase 3, where the trap
  bites.
- **Windows verification not yet run** (§7 asks for one pass per phase). No
  Windows machine is available in this environment; the CI job renders on Linux,
  which exercises the same Fusion path. Flagged, not silently skipped.

### Phase 1 — Main window shell ✅

Both toolbars, the board-count row, the status line, the log pane, the
single-instance lock and window-geometry persistence. The part table is present
with its nine column headers and no rows.

- `ui/main_window.py` — the §5.1 layout. `ui/icons.py` reuses the existing
  `icons/` set, recolouring the black line art for dark mode.
  `ui/log_pane.py` marshals log records onto the UI thread through a queued Qt
  signal (the replacement for `wx.CallAfter`).
- `single_instance.py` — a `QLocalServer` whose *binding* is the lock and which
  doubles as the "come forward" channel. Scoped **per board**: KiCad reruns the
  launcher on every toolbar click, so there is no window to look for, and two
  windows on one board would share a project database.
- Screens: `mainwindow.png`, `mainwindow-dark.png`.

Deliberate departures from the wx layout, all visible in the screenshot:

- **The right-hand toolbar is 152px, not 128px.** At 128 the wx original elides
  "Assign LCSC number" to "Assign … number". 24px of table width buys ten
  readable labels.
- **All ten per-part buttons fit.** §5.1 records that the wx toolbar is "long
  enough that a button at its end is scrolled out of sight"; Qt does the same
  thing with an extension arrow, which quietly loses `Save mappings`. Tightened
  button padding plus a 128px log pane (down from wx's 150) makes all ten
  reachable, and `test_every_part_button_fits_without_an_extension_arrow` keeps
  it that way.
- **The table and log are in a splitter.** Same default proportions, draggable.
- The label is `Toggle BOM & POS`, as the running plugin has it; §5.1 writes it
  without the ampersand.

Known limitation: `restoreGeometry` clamps to the screen, and the offscreen
platform's virtual screen is 800x800, so the geometry test asserts height only.
`resize` does not clamp, which is why screenshots still come out at 1300x772.

### Phase 2 — Part table ✅

The table is real: nine columns over `store.py`, all of them filled, row
colouring, sorting, multi-select, the row context menu, the BOM/POS toggles and
match highlighting. The `Store` adapter question that Phase 1's note flagged is
settled.

Landed:

- **`lcsc_suite/ui/models/part_table.py`** — `PartTableModel`. Column indices are
  named constants (`REF`, `STOCK`, …). `PartRow` is the displayed shape, distinct
  from the store's persisted dict. Two custom roles: `SORT_ROLE` (so JLC Stock
  sorts numerically, with an unknown at `-1` so it lands *below* a confirmed
  zero) and `REFERENCE_ROLE` (find a row from a selection without caring which
  column was clicked). `?` for "nobody answered", `0` for "a source said none",
  and **blank** for a part with no LCSC number at all — a `?` there reads as a
  failed lookup when there is nothing to look up.
- **`lcsc_suite/parts.py`** — `PartList`, the board↔database↔rows reconciler
  (the wx plugin does this inside `mainwindow.populate_footprint_list`).
  `_StoreOwner` adapts our `Settings` to the `parent.settings` mapping that
  `store.py` and `library.py` reach into, plus the `project_path` and
  `post_event` attributes `library.py` also reaches for.
- **`store.py` gained a toolkit-free seam.** Rather than making `FootprintView`
  quack like a pcbnew footprint, `update_from_board()` is now a thin adapter that
  builds plain part records and calls the new **`update_from_parts(parts)`**,
  which owns all the reconciliation rules (the `lcsc_priority` setting; "value or
  footprint changed, so the number is no longer trustworthy"). Same split for
  `backfill_estimator_metadata(footprint, …)` → **`backfill_part_metadata(part,
  …)`**, and `clean_database(references=None)`. `Store(parent, path, board=None)`
  skips the board read, which is what the Qt app passes.
  `tests/test_store_pad_filter.py` still calls the pcbnew-facing spellings, and
  they still work.
- **Sorting is a `QSortFilterProxyModel`**, not `store.set_order_by`'s SQL, so a
  header click cannot disagree with what the database returned.
- **`FixtureBoard.relocate(project_path)`** — the committed fixture names a path
  that does not exist on purpose, but `store.py` really does create
  `<project>/jlcpcb/project.db`, so the probe and the tests point it at a temp
  directory first.
- Screens: `mainwindow.png` plus **`mainwindow-unassigned.png`**, a second view
  scrolled to the first part that needs a number — the default view does not
  reach one, and the row colouring is the thing most worth looking at. In it
  `G1`–`G3` and `JP1` are red and bold; the mounting holes are excluded from the
  BOM and deliberately *not* marked.
- **`lcsc_suite/parts.py` opens the part libraries.** `open_library(owner)` builds
  `library.Library` over the data directory the wx plugin already fills, so while
  both halves are installed they share one part cache, one mappings table and one
  corrections database. It returns `None` rather than raising — an unreadable
  data directory costs three columns, which is not a reason for the window to
  refuse to open — and `PartList.open_libraries()` is the app's call site.
  `Library` gained one parameter for this: **`allow_network`**, defaulting to
  `True` so the wx plugin's behaviour is unchanged, and passed `False` here. The
  only thing construction can do over the wire is seed a global corrections
  database that does not exist yet; that belongs to Phase 5, which owns the
  Corrections dialog, not to a part list opening.
- **Type / LCSC Params / JLC Stock now fill**, from the local cache only. Never
  the network: `_details()` runs once per assigned part while the list is built
  on the UI thread, and serving a stale row unconditionally is what makes an
  offline session work. Refreshing is Phase 4's job.
- **Match highlighting** — `lcsc_suite/ui/delegates.py`, a `QStyledItemDelegate`
  on the LCSC Params column, plus `MATCH_TERMS_ROLE` on the model.

  It is worth being clear about what this feature *is*, because the setting is
  labelled "Highlight search matches" and there is no search box in this window.
  The terms are the row's **own value and footprint**, so the highlight marks
  where the derived LCSC parameters corroborate what the board declares: a
  `100K` in an `R_0402_1005Metric` lights up `100kΩ` and `0402` inside
  `100kΩ ±1% 0402`. **A row with nothing lit is one where the two disagree** —
  visible in `mainwindow-unassigned.png`, where the `J1`–`J4` terminal blocks
  highlight nothing at all.

  `expand_value` / `expand_footprint` are **imported from
  `dataview_highlight.py`, not reimplemented**: which spellings count as the same
  thing (`390R` is `390Ω`, `10uF` is `10µF` is `10µ`, `R_0402_1005Metric` is
  `0402`) has a long tail, and a missed equivalence shows up only as a cell that
  never highlights. Only that module's wx renderer is left behind. The delegate
  is a third the size of the wx original because none of what that file works
  around exists in Qt.
- **A new `match` colour**, deliberately not the `standard` amber. A
  standard-mode trigger colours a whole row and means "this costs more"; a match
  tints runs inside one cell and means "this corroborates". They co-occur, and
  one colour for both would read as one meaning — the mistake red and amber made
  once already.
- Screens: `mainwindow.png` plus **`mainwindow-unassigned.png`**, a second view
  scrolled to the first part that needs a number — the default view does not
  reach one, and the row colouring is the thing most worth looking at. In it
  `G1`–`G3` and `JP1` are red and bold; the mounting holes are excluded from the
  BOM and deliberately *not* marked. `R3` carries a number but no cached details,
  so it shows `?` next to real figures — the two states in one picture.
- Tests: `tests/test_qt_part_table.py` (40) covers the `?`/`0` distinction in
  both the cell and the sort, the red-vs-amber meanings, board-before-database
  write ordering, a trapped board leaving the database alone, the three columns
  filling from a seeded cache, and the highlight terms and spans.

**Still open, and each blocked on a later phase** — none of these is work that
can be finished here:

1. ~~**The context menu is wired but inert.**~~ **Closed in Phase 3**, which
   answered the design question with `controller.py`. The guess below that the
   two mapping entries needed Phase 5 was wrong — they open no dialog — and they
   landed in Phase 3 too; only the three "Add correction …" entries still wait
   for Phase 5. Original note: `MainWindow._on_context_menu` emits
   `row_menu_triggered(entry_id, references)` for the ids in
   `main_window.ROW_MENU`; nothing is connected to it yet. Deciding *where* the
   dispatch lives is the open design question — probably a controller object that
   owns `PartList`, the window and the dialogs, rather than the window growing
   handlers. Phase 3 is the phase that has to answer it.
2. **`highlight_standard_parts` is read but nothing sets the trigger refs.**
   `PartTableModel.set_standard_trigger_refs()` is called by nobody until the BOM
   estimator lands (**Phase 5**), so the amber advisory is unreachable through
   the UI. Tested directly.
3. **Nothing can toggle match highlighting at runtime.** The delegate reads
   `highlighting.matches` when the window is built and exposes `set_enabled()`;
   the checkbox that would call it is in the Settings dialog (**Phase 5**).
4. **Live IPC verification is still deferred.** Everything is proven against
   `FixtureBoard`. KiCad quit during the Phase 0 session and has not been
   reopened, so no write has yet gone over a real socket. Do this in **Phase 3**
   against a **copy** of a board in the scratchpad, never the user's own.
   *(Still open after Phase 3 — KiCad was not running for that session either.
   See Phase 3's item 1, which is now the oldest outstanding item.)*

### Repository reorganisation (between Phase 2 and Phase 3)

Not a phase — a layout change made because the file paths in §3's tables were
about to be typed a great many more times, and they were wrong in a way that
cost real work.

**The legacy plugin moved from the repository root into `kicad_lcsc_suite/`.**
Everything §3 lists — the wx modules, the shared logic, `lcsc/`, `dblib/`,
`bom_estimation/`, `enrichment/`, `icons/`, `lib/`, `VERSION` — is under that
directory now. Prefix any path in §3 or §5 with it. `common/` went the other
way, into `db_build/common/`, because the build Action is its only consumer.

Why it was worth doing mid-migration:

- **Three synthetic package names disappeared.** The root's directory name has
  hyphens, so nothing could import it. `shared.py` faked a package with
  `importlib`; the tests faked `kicadplugin`; `test_store_pad_filter.py` faked
  `kicad_jlcpcb_tools`; ten files carried `sys.path` preambles. All of it is now
  `import kicad_lcsc_suite.x` plus one line in `tests/conftest.py`.
- **`install.sh` was symlinking the entire checkout** into KiCad's plugin
  directory — `.venv/`, `docs/`, `tests/` and the Qt app included. It links one
  package directory now, and `PCM/create_pcm_archive.sh` copies one directory
  instead of enumerating files and dirs by hand.
- **Phase 8 becomes a deletion** rather than an excavation: promote the surviving
  logic modules into `lcsc_suite/`, then remove `kicad_lcsc_suite/`.

Three things the move broke, all found and fixed — worth knowing because the
same shapes will recur at Phase 8:

1. **`__init__.py` imported the whole wx UI as a registration side effect.**
   Harmless while the package could not be imported by name; once it could,
   every logic import dragged in `mainwindow.py`. Registration is now guarded on
   a *real* (file-backed) `pcbnew`, so a stub does not trigger it.
2. **The icon path silently broke and nothing failed.** `icons.icon()` returns an
   empty `QIcon` rather than raising, so every toolbar button lost its image and
   the screenshots looked like a deliberate restyling. Caught by pixel-diffing
   against the committed PNGs — 49% of pixels differed. `shared.LEGACY_ROOT` now
   anchors the icon set and the wx settings file, `icons.py` logs loudly if the
   directory is missing, and `test_every_toolbar_button_has_its_icon` fails if a
   button renders bare.
3. **Test isolation was accidental.** Each test file used to load its own copy of
   a module under a private package name; sharing real modules exposed
   import-time captures (`from pcbnew import GetBuildVersion`, `pcbnew.F_Cu`, the
   stubbed `get_is_dnp`) that raced on collection order. Fixed by stubbing with
   `setdefault` and pinning values on the imported module. All 512 tests now pass
   forward, in reverse, **and one file at a time** — the last of which never
   worked before.

Screens were re-rendered and are pixel-identical to the Phase 2 commit apart
from the log pane's timestamps, which are not deterministic between runs.

`docs/CODE-REVIEW.md` is deliberately **not** updated: it records a review of a
stated baseline (2026-08-03) and rewriting its paths would misrepresent what was
reviewed.

### Phase 3 — Assignment path ✅

`Assign LCSC number`, `Remove LCSC number`, `Auto-select alike` and
`Save mappings` all write, and the row menu that had been emitting into nothing
since Phase 2 now dispatches. Phase 2's open design question is settled.

**The answer to "where does the dispatch live" is `lcsc_suite/controller.py`,**
and the rule it settles on is one line:

> **The window builds, displays and reports. The controller decides and writes.**

`SuiteController` owns the `PartList`, builds the `MainWindow` and holds every
call that changes the board, the project database or the mappings table. The
window keeps its layout, its selection, its model and the *appearance* of the row
menu. `MainWindow._toggle_exclusions` moved here to make the split real — leaving
half the writes in the window would have made the answer meaningless.

Landed:

- **`lcsc_suite/controller.py`** — `SuiteController`. `assign_number()` is the
  **single funnel** every source of a number goes through: the dialog,
  `Paste LCSC`, `Find mapping`, and the Explorer in Phase 4. That is deliberate —
  the wx plugin writes the same eight lines in four entry points
  (`assign_parts`, `paste_part_lcsc`, `search_foot_mapping`,
  `import_from_schematic`) and they have already drifted.
- **`PartList.assign()` / `.clear()`** — board first, database second, same rule
  and same reason as `set_exclusions`. `clear()` takes the stock figure with it.
  `stock` passes through as given, `None` included, because `None` is "nobody
  answered" and `0` is "a source said none". Type and params are deliberately
  *not* written: they are cache-derived and `rows()` re-resolves them on the next
  rebuild, which is why an assignment fills three columns with no second action.
- **`kicad_bridge.sanitize_lcsc()`** — the wx `sanitize_lcsc` regex, moved next
  to `LCSC_VALUE_PATTERN` because that is where "what is an LCSC number" already
  lives. Text with no number in it returns `""`, and **every caller treats that
  as "do nothing", never as "clear"** — a failed paste must not read as a
  removal.
- **`lcsc_suite/ui/assign_dialog.py`** — `AssignNumberDialog`. OK stays disabled
  until the text contains a number, and a paste that resolves to something
  different says which number it found.
- **`PartList.remember_mappings()` / `.mapped_numbers()`** — the mappings table,
  shared with the wx plugin, so both halves see each other's entries. A row
  missing footprint, value *or* number is skipped rather than stored blank: a
  mapping keyed on an empty footprint would match every part without one.
- **`MainWindow.set_row_menu_enabled()`** — the controller declares which entries
  it answers; the rest are greyed out, not removed.
- Screens: `assign-dialog.png` and **`mainwindow-assigned.png`**, the counterpart
  to `mainwindow-unassigned.png` — the same four rows, assigned. Red and bold
  gone, the LCSC column filled, Type / JLC Stock / LCSC Params re-resolved from
  the cache, and the write in the log pane.
- Tests: `tests/test_qt_assignment.py` (64). 528 → 592 overall.

Departures from the plan and from the wx plugin, all deliberate:

- **The assign dialog is new.** The wx plugin's `Assign LCSC number` opens the
  Explorer, which is Phase 4, so a phase boundary needed *some* source of
  numbers. It is not a new capability — `Paste LCSC` is already manual number
  entry with a clipboard instead of a text field — and it earns its keep past
  Phase 4, because someone who knows the number should not have to search for
  it. **Phase 4 makes the Explorer a second caller of `assign_number()`, not a
  replacement for this.**
- **`Find mapping` and `Add mapping` landed here, not in Phase 5.** Phase 2's
  note said the two mapping entries needed Phase 5's dialogs. That was wrong:
  `add_foot_mapping` and `search_foot_mapping` open no dialog in the wx plugin —
  they act on the mappings table directly, which `Save mappings` already needed.
  Only *`Manage mappings`* is a dialog. The three `Add correction …` entries do
  need Phase 5 and are the only greyed-out entries.
- **`Copy LCSC` on a multi-row selection now keeps every distinct number**,
  newline-separated. The wx version reopens the clipboard once per selected row
  and therefore keeps whichever row it visited last; that is a loop written for
  one row, not a decision. Single-row behaviour is unchanged, and paste still
  takes the first number it finds.
- **`Find mapping` writes one commit per distinct number**, not one per row, so
  twenty identical capacitors are one undo step.

One probe bug found and fixed while adding the screens, worth knowing because it
would have silently degraded every future screenshot: **`open_board()` was called
once per theme and the fixture board is mutable.** `mainwindow-assigned` writes
to it, and `mainwindow-unassigned` renders after it alphabetically — so the
"unassigned" screen had nothing unassigned left to show. Boards are now opened
per screen, for the same reason settings already were.

#### Live IPC verification — done, and it found a real bug

The item deferred since Phase 0 finally ran, against KiCad 10.0.3 with a
**copy** of the real board (`~/Research/temperature-controller/PCB/PCB_new.kicad_pcb`)
opened from the scratchpad. It failed on the first write, and it failed for a
reason no fixture could have shown:

> `WriteVerificationError: KiCad reported success but the board did not change:`
> `J1: lcsc is 'C8465', expected 'C99999'`

That is **trap 4** (now in §2): a read cannot see an open commit, so
`apply()`'s original order — mutate, verify, then push only if the board
agrees — verified against the pre-commit state every single time and could
never have succeeded over a real socket. Two phases of green tests had passed
over it, because `FixtureBoard` applied writes to its committed state
immediately.

What changed as a result:

- **`_Board.apply` was reordered**: snapshot → commit → push → verify →
  restore-on-mismatch. `_restore()` puts the snapshot back in a second commit
  and reports whether that worked; the error message now tells the user which
  of the two states their board is in, because that is their next question.
- **`FixtureBoard` now stages writes in `_pending` and reveals them on
  `_push`**, so it reproduces trap 4 as faithfully as it already reproduced
  trap 2. `test_an_open_commit_is_not_visible_to_a_read` pins it.
- **Two tests changed meaning, correctly.** "A failed write leaves no commit"
  is no longer achievable and is now "a failed write puts the board back", plus
  `test_a_failed_write_costs_two_undo_entries` to state the price deliberately.

After the fix, the live run passes end to end: updating an existing field,
creating one where the footprint had none (trap 3, hidden as intended), a
three-reference batch, an exclude-from-BOM flip, and the rejection of a
non-number — each asserted by re-reading, each restored afterwards. The whole
app was then rendered against the live board with
`scripts/qt_probe.py mainwindow --live`, which is the first screenshot of this
app driven by real IPC rather than the fixture.

**The lesson worth carrying to Phase 4:** a fixture is only evidence to the
extent that it is *less* permissive than the thing it stands in for. This one
had been more permissive in exactly one respect, and that respect was where the
bug was.

**Still open:**

1. **The three `Add correction …` row-menu entries** need Phase 5's Corrections
   dialog. They are greyed out, and `HANDLED_ROW_MENU` is what says so.
2. **`schematic_cleared_refs` and `schematic_sync_pending` are maintained but
   read by nobody.** Phase 7 consumes them. They are tracked from here because
   only the assignment path knows a removal happened, and the distinction they
   carry — a reference *deliberately cleared* versus one merely blank — cannot be
   reconstructed later.
3. **Windows verification still not run** for any phase.

### Undo — found by using it, not by testing it (2026-08-07)

Not a phase. A bug report from the first real session with the assignment path:
`Remove LCSC number`, then Cmd+Z, and the numbers did not come back.

`kicad_bridge` was not at fault — `apply()` does put exactly one entry in
KiCad's undo history per action, and §6's Phase 3 notes are still accurate.
**The mistake was assuming that was the whole job.** Three things were wrong at
once, and no test could have caught any of them, because every one of them lives
in the gap between the two processes:

1. **The keystroke went nowhere.** This window bound `Ctrl+W`, `Ctrl+Q` and
   `Shift+Esc` and nothing else. Right after clicking a button *in our window*,
   our window has focus — so Cmd+Z was delivered here and matched no action at
   all. KiCad's undo history was never asked.
2. **KiCad's undo cannot reach the project database.** A removal clears the board
   field *and* the number and stock figure in `project.db`. Undoing the board
   half in pcbnew leaves the database still saying unassigned — and the part
   table reads the database, so even a *working* undo would have looked like a
   failed one.
3. **Nothing re-reads the board after an external change.** `PartList`
   reconciles on our own writes. A change made in pcbnew, an undo included, is
   invisible here until something asks the board again.

So the app has its own undo, and it is the only one that covers everything an
action changed. **`lcsc_suite/undo.py`** is an `UndoStack` of
`(description, revert)`; `SuiteController` records one entry per action and
`undo_last()` performs it.

- **A reversal is a new verified write, not a rollback.** It goes through
  `apply()` like anything else, so trap 2 applies to it and it costs its own
  entry in KiCad's history. Reversing therefore leaves *two* KiCad entries rather
  than zero — the honest price of also being able to put the database back.
- **A refused reversal keeps its stack entry.** The board is unchanged after a
  refused write, so the reversal is still valid; losing it would leave no way
  back at all.
- **`Undo` is a labelled button, first in the top-left group, plus
  `QKeySequence.StandardKey.Undo`** (Cmd+Z on macOS, Ctrl+Z elsewhere). The
  button exists because the keystroke is genuinely ambiguous with two windows
  open, and a tooltip naming the action is the only place that ambiguity can be
  explained. The label stays `Undo`; the action being reversed goes in the
  tooltip, so the toolbar does not shuffle sideways after every change.
- **Batching matches the forward write.** A batch reverses in one commit, and
  `_grouped()` makes `Find mapping` — one user action, up to one commit per
  distinct number — reverse in one press.
- **`schematic_cleared_refs` is restored too.** A reference whose removal has
  been reversed is not a deliberate removal any more, and Phase 7 cannot
  reconstruct that distinction later.
- Not reversible, deliberately: the mappings writes (`Save mappings`,
  `Add mapping`). They are additive, project-independent and harmless, and they
  do not clear the stack either.

Two things worth knowing beyond the fix:

- **`store.create_part` defaults the stock column to `''`, not SQL `NULL`.**
  Snapshotting the raw value and handing it back to `assign()` raises on
  `int('')`. `part_table.as_stock` was already the rule for this and is now
  public rather than `_as_stock`; `test_an_unknown_stock_figure_survives_a_reversal`
  pins it. Found by a test, which is the one of these that testing did catch.
- **Whether KiCad's own undo covers an IPC write is still unanswered**, because
  KiCad was not running for this session. `live_ipc_check.py` gained a sixth
  check that asks it directly via `run_action("common.Interactive.undo")` and
  **reports rather than asserts** — the answer belongs to KiCad, the action names
  are explicitly unstable, and the app is correct either way.
  `_Board.run_kicad_action` exists for that check alone; nothing in the app calls
  it.

New art goes in **`lcsc_suite/icons/`**, which `ui/icons.py` now searches before
the legacy set. `mdi-undo.png` is the first entry. Phase 8 deletes
`kicad_lcsc_suite/`, and anything of ours left inside it would go too.

Tests: `tests/test_qt_undo.py` (28). 595 → 623 overall. Screens re-rendered:
`mainwindow.png` shows the button greyed with nothing to reverse,
`mainwindow-assigned.png` shows it live.

### Phase 4 — LCSC Explorer ✅

The biggest single piece. Counting the four wx modules it replaces
(`explorer.py`, `photoviewer.py`, `previewpanel.py`, `facetfilter.py`): **3601
lines / 2285 of actual code → 3033 lines / 1940 of code**, spread across eight
files instead of four. A 15% reduction, which is smaller than it feels while
writing it — the workarounds listed below did go, and the prose explaining *why*
they went took their place. The win is not the line count; it is that none of the
deleted code had anything to do with LCSC parts.

`lcsc/explorer.py` → **`lcsc_suite/ui/explorer/`**:

| Module | What it owns |
|---|---|
| `window.py` | the dialog: search, filters, the two fills, the action bar |
| `results.py` | the grid's model, its four delegates and the column fitting |
| `facets.py` | the parametric filter panel and the tick debounce |
| `detail.py` | the selected part's pane, both stock cards, both layouts |
| `preview.py` | the symbol / footprint / photo tiles (`lcsc/previewpanel.py`) |
| `tasks.py` | `QThreadPool` pools, the staleness tokens, the fill caps |

Plus **`lcsc_suite/ui/photo_viewer.py`** (`lcsc/photoviewer.py`) and
**`lcsc_suite/search_source.py`**, which is new and is described below.
`lcsc/facetfilter.py`'s `ComboCtrl`+`CheckListBox` popup became a `QToolButton`
with a checkable `QMenu`.

Screens: `explorer`, `explorer-detail`, `explorer-inline`, `explorer-retail`,
`explorer-facets`, `photo-viewer`, each in both appearances.
Tests: `tests/test_qt_explorer.py` (48). 623 → 671 overall.

#### The search fixture, and what it is really for

`lcsc_suite/fixtures/explorer/` holds one live capture, taken 2026-08-07 under
the authorisation the pre-flight notes recorded: keyword `10nF 0402`, 100 hits of
18,327 matches, JLC assembly detail and LCSC retail detail for **all 100** rows,
EasyEDA product records for 6, and 93 thumbnails. Written by
`scripts/capture_explorer_fixture.py`, which refuses to run without
`--capture-once`, paces every request and stops after five refusals in a row.
Every host answered; nothing had to be hand-built.

**Correcting the pre-flight note:** it said `qt-screens.yml` "asserts the
committed PNGs match what renders", and that a live grid would therefore fail the
gate permanently. That overstates it — the job compares **presence and image
size**, not pixels, because font rasterisation differs between the runner and a
developer's machine. Changing stock figures would not fail it. The fixture is
still right, for three reasons that survive the correction:

- LCSC 403s whole networks and GitHub's shared runner ranges are prime
  candidates, and `_HostBreaker` keeps a blocked run blocked for ten minutes —
  so the screens would *raise*, which the gate does catch;
- a screenshot whose contents change every run is not evidence a human can
  review, which is the entire point of committing them;
- a test asserting on live stock asserts on the figure whose disagreement
  across sources is the thing this fork exists to *show*.

**The capture is raw payloads and replays through `api.py`'s own parsers.** It
carries no `SearchHit` objects and no post-processed shapes. That is the direct
lesson of trap 4: a fixture is only evidence to the extent it is *less* permissive
than the thing it stands for, and one invented from the shapes we think an
endpoint returns is more permissive by construction. Here a change to how
`api.py` reads a field changes what the fixture produces, because it is the same
code reading it. Gzipped — 9 MB of raw retail payloads compress to 416 kB —
rather than trimmed to the fields currently read, because trimming is exactly the
post-processing the rule forbids.

**Offline is structural, not a matter of timing.** `_get_json` and `fetch_image`
both consult the cache first and the host breaker second, *before* any socket. So
`FixtureSource` primes a never-expiring cache with the captured payloads and
installs a breaker that reports every host tripped open: a captured URL is served,
an uncaptured one takes the module's own "nobody answered" branch. No edit to
`api.py`, which is copied and not edited.

#### Three bugs the phase found, and how

Worth recording individually because each was found by a different instrument.

1. **The whole LCSC retail column rendered `…`, under a status line saying both
   hosts were refusing.** *Found by looking at the screenshot.* The breaker
   stand-in refuses everything, so `api.retail_unreachable()` — which asks the
   breaker per host — reported both retail sources down and the Explorer skipped
   the fill it was about to run. The breaker is still right to refuse everything;
   it is the transport block. But "can this process open a socket" and "does this
   source have retail data" are different questions, and only the second is what
   the window asks. `FixtureSource.retail_unreachable()` answers it.

2. **The offline guarantee had a hole, and it was in `api.jlc_search`.** *Found by
   a test.* When the direct POST yields nothing, `jlc_search` falls back to the
   vendored `easyeda2kicad` client — which carries its own transport and never
   passes the host breaker. A keyword the capture does not hold went straight out
   to the network from a source whose entire contract is that it cannot.
   `FixtureSource.search` calls `_jlc_search_direct` by name, which uses the same
   cache key and has no fallback. `test_the_search_never_falls_back_to_the_
   vendored_client` pins it shut.

3. **The availability card read "expand part".** *Found by looking at the
   screenshot.* `SearchHit` maps the wire spelling to Basic/Extended;
   `StockReport` deliberately does not, because it reports what the endpoint
   said — and the card preferred the report. The wx original has the same bug.
   Fixed in the UI (`detail.library_label`), not in `api.py`.

#### What got smaller, and why

Every item here is a wx workaround with no Qt counterpart, which is the migration's
thesis stated in code.

- **The column-width machinery: ~200 lines → one function.** `_resize_columns`
  (67), `_measure_grid_metrics` (60), `_squeeze` (22), `_set_column_hidden` and
  the `FLEX_WEIGHTS`/`SHRINK_ORDER`/`MIN_SHARE` tables (~40) existed because the
  macOS DataView redistributes every width when one changes and adds per-column
  padding it will not report — ignoring which once overflowed a header by 135px.
  `results.fit_columns` is about twenty lines of logic: `QTableView`'s viewport
  reports its own width honestly and a column stays where it is put. The
  throwaway trailing `spacer` column is deleted rather than ported.
- **`_post()` / `_alive()`: gone.** A Qt signal to a destroyed receiver is not
  delivered and does not raise, because Qt severs the connection. The wx pair
  existed because `wx.CallAfter` raises *on the worker thread* once the dialog is
  gone, where nothing catches it. **The staleness tokens stayed** — auto-
  disconnection answers "the window is gone" and says nothing about "these results
  are for the previous search".
- **"Inline below" is now a real row.** The wx version could not put a child
  window inside a native DataView, so the pane was a *sibling* clipped to the
  rectangle of reserved placeholder rows and repositioned by a 100ms timer
  (`INLINE_TRACK_MS`, `inline_clip`, `_position_inline_detail`, `_inline_placed`)
  because that control's scroll notifications are not dependable. Here the model
  inserts a placeholder row, the view spans it across every column and
  `setIndexWidget` puts the pane in it. It scrolls because it *is* a row. The
  timer and its three helpers are deleted.
- **Renderers are not told their own width.** `set_cell_width` on three wx
  renderers existed because a custom renderer is handed the rect it asked for in
  `GetSize()`, not its column's — which once wrapped "Multilayer Ceramic
  Capacitor" inside a 100px box in a 470px column. A `QStyledItemDelegate` is
  handed `option.rect`.
- **The thumbnail lives in the model.** wx could not hold an invalid `wx.Bitmap`
  in a `DataViewListCtrl`, so the cell value was the LCSC code and the renderer
  looked artwork up through a callback. `QPixmap` has a null state.
- **One repaint per thumbnail, not per batch.** `_thumb_refresh_scheduled` and
  `_flush_thumb_refresh` coalesced arrivals because a DataView has no single-cell
  invalidation. `dataChanged` on one index does.
- **The parameter table's `EVT_SIZE` handler is gone.** Two fixed widths could not
  serve both layouts; `ResizeToContents` plus a stretched value column can.

What deliberately did **not** change: the fetch ordering, the fill caps
(`RETAIL_FILL_LIMIT` 120 / 2 workers, `THUMB_FILL_LIMIT` 60 / 3 workers, and the
reason two rather than five), `FILTER_DEBOUNCE_MS`, the OR-within/AND-across facet
semantics, and the `…` / `?` / `0` distinction.

#### Departures from the plan and from the wx plugin

- **§5.2's Inventory selector is stale.** It lists three options; the running
  plugin has two. "Both inventories" was removed before this migration started —
  it meant a hundred extra per-part lookups per search, re-fired on every filter
  change, which LCSC answers with a 403 in some regions and EasyEDA answers with
  a ban. Two is what was ported. The *three inventories* the pre-flight note
  worried about are the three **sources** (JLC assembly, LCSC retail, the EasyEDA
  fallback), and those are all present.
- **The photo falls back to JLC's file service.** The wx version tried only
  `report.images`, which are LCSC's own CDN URLs — and LCSC 403s whole networks
  taking that CDN with it, which is precisely why §4 says photos come from JLC.
  `ExplorerWindow._photo_urls` appends `hit.photo_url` and `hit.thumbnail_url`
  behind the report's own, so a blocked CDN costs sharpness rather than the
  picture. A UI change, which is where §4 says such changes belong.
- **The Explorer does not write.** It emits `assign_requested(number, stock)` and
  `SuiteController.assign_number` performs it — the same funnel the dialog,
  `Paste LCSC` and `Find mapping` already go through. Library *import* is done in
  the window: it writes files under a folder this window owns the field for and
  touches neither the board nor the project database.
- **One Explorer, re-targeted.** `SuiteController.open_explorer` raises and
  re-points an open one. Two would each hold a search, two fills and a photo
  window, and both would write through the same controller.
- **The photo tile's caption is "Photo (enlarge)".** At 140px the wx wording
  elided to "to (click to enla", which advertises nothing.

#### Known limitations, stated rather than skipped

- **`?` does not appear in any Phase 4 screenshot.** The capture answered for
  every row, so nothing is in the "asked, nobody answered" state — that is what
  the data honestly is, not a fixture that flattened it. `0` *does* appear (28 of
  100 rows have zero LCSC retail stock, visible in `explorer-retail`), and all
  three spellings are covered directly by tests.
- **Library import is not covered by the offline guarantee.** `LcscImporter` uses
  the vendored `easyeda2kicad` network client, which has its own transport. The
  probe and the tests never invoke it, so nothing currently reaches the wire —
  but a future test that clicks Import would. Route it through `search_source` if
  that day comes.
- **Windows verification still not run**, for any phase. No Windows machine here.
- **`live_ipc_check.py` was not re-run**, because `kicad_bridge` was not touched
  in this phase. Re-run it whenever it is.

### Resume here → Phase 5

Read this whole §10, then `git log --oneline` for the phase commits. Every screen
in `docs/screens/` is current, and Phases 0–4 are done.

**Phase 5 is the remaining dialogs**: Settings (§5.3), Corrections (§5.4),
Mappings (§5.5), Part Details (§5.6) and the BOM estimator (§5.8). It is five
small windows rather than one large one, and three things are waiting on it:

1. **The three `Add correction …` row-menu entries.** Greyed out since Phase 3;
   `controller.HANDLED_ROW_MENU` is what says so. They need the Corrections
   dialog.
2. **`PartTableModel.set_standard_trigger_refs()` is called by nobody**, so the
   amber standard-mode advisory is unreachable through the UI. The BOM estimator
   is what sets those references.
3. **Nothing toggles match highlighting at runtime.** The delegate reads
   `highlighting.matches` at build time and exposes `set_enabled()`; the checkbox
   that would call it lives in the Settings dialog.

Also still open, and older: **`schematic_cleared_refs` and
`schematic_sync_pending` are maintained but read by nobody** — Phase 7 consumes
them.

House rules that have been followed so far and should keep being followed:

- **Never claim a UI change works without looking at the PNG.**
  `.venv/bin/python scripts/qt_probe.py --all --theme both` and then actually
  read the images. `--geometry` is a supplement, never a substitute. Commit the
  PNGs in the same commit as the UI change. Phase 4 found two real bugs this way
  that no test caught, and one that only a test caught — the two instruments do
  not overlap.
- `scripts/qt_probe.py --list` names every screen; add a `screen_*` builder plus
  an entry in `SCREENS` and CI covers it automatically. Give each screen fresh
  `probe_settings()` — the main window saves its geometry on close and the next
  screen would restore it.
- **The probe and the tests must never touch the wire.** `search_source.
  build_source()` defaults to live; the probe and the tests pass a
  `FixtureSource`. Same shape as `Library(allow_network=False)` from Phase 2.
- `lcsc_suite/shared.py` is the only sanctioned way to import
  `kicad_lcsc_suite`'s logic modules, and `shared.LEGACY_ROOT` the only way to
  reach its *data* (`icons/`, the wx `settings.json`). No `sys.path` hacks.
- `lcsc/api.py` is **copied, not edited**. If a UI need seems to require an API
  change, change the UI — Phase 4 did exactly that three times.
- The board fixture (`lcsc_suite/fixtures/board.json`) has 110 footprints, 93
  assigned, 8 excluded from both BOM and POS, and `R12` marked DNP.
  `FixtureBoard(honour_footprint_writes=False)` reproduces trap 2 and staged
  `_pending` writes reproduce trap 4.
- Run `.venv/bin/python -m pytest -q` at every phase boundary. 436 at the start of
  the migration, 671 now. Every file must also pass **on its own**:
  `for f in tests/test_*.py; do .venv/bin/python -m pytest -q "$f" >/dev/null || echo "FAILS ALONE: $f"; done`
- `ruff check --extend-exclude=lib` must pass. `ruff format --check` still reports
  the same four **untouched upstream** files (`corrections.py`, `events.py`,
  `kicad_drc.py`, `partdetails.py`) — leave them alone, as CLAUDE.md says.
- **Re-run `scripts/live_ipc_check.py` whenever `kicad_bridge` is touched.** It
  needs KiCad open with a **copy** of a board and refuses a project path that does
  not look disposable:

  ```bash
  mkdir -p /tmp/lcsc-live
  cp ~/Research/temperature-controller/PCB/PCB_new.kicad_pcb /tmp/lcsc-live/livecheck.kicad_pcb
  open -a "/Applications/KiCad/PCB Editor.app" /tmp/lcsc-live/livecheck.kicad_pcb
  .venv/bin/python scripts/live_ipc_check.py
  ```

  Close the board **without saving** afterwards.
- **Do not re-run `scripts/capture_explorer_fixture.py`** unless the payload shape
  has actually changed. It spends live requests against hosts that rate-limit and
  geo-block, and the fixture it writes is already committed.
- The legacy wx plugin stays installed and working until Phase 8.
  `./install.sh --list` shows both halves.

Environment notes worth knowing:

- `./install.sh --app` has been run; the IPC plugin is symlinked at
  `~/Documents/KiCad/10.0/plugins/lcsc_suite` → `kicad_plugin/`, and the venv is
  `.venv` (Python 3.14.5, PySide6 6.11.1, kicad-python).
- KiCad's API server is enabled. The launcher's log goes to
  `~/.local/state/lcsc-suite/plugin.log` — the only place a start-up traceback can
  be read, because KiCad discards both streams.
- Settings live at
  `~/Library/Preferences/kicad-lcsc-suite/LCSC Suite/settings.json`.
- The offscreen platform's virtual screen is **800x800**. `resize()` ignores it
  (so screenshots come out at their stated size) but `restoreGeometry()` clamps to
  it, which is why the geometry test asserts height only.
- Reachability changes by the day. One line to check before blaming the UI:
  `curl -o /dev/null -w '%{http_code}\n' 'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592'`
  It answered 200 for the Phase 4 capture, which the standing note says is the
  exception rather than the rule.
