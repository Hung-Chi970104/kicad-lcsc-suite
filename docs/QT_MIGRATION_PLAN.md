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

### Three traps found in the spike — read before writing code

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
