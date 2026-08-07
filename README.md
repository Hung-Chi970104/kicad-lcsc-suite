# kicad-lcsc-suite

One KiCad plugin for everything LCSC: parametric search, honest dual stock,
symbol/footprint/3D import, and JLCPCB fabrication output.

This is a **fork** of [Bouni/kicad-jlcpcb-tools][bouni] with
[uPesy/easyeda2kicad.py][e2k] vendored in and three capabilities added that
neither tool had on its own. Upstream commits are pinned in `UPSTREAM.txt`.

[bouni]: https://github.com/Bouni/kicad-jlcpcb-tools
[e2k]: https://github.com/uPesy/easyeda2kicad.py

---

## Why this exists

Three separate tools each did part of the job:

| Tool | Search | Parametric filters | Symbol/footprint import | Stock shown |
|---|---|---|---|---|
| kicad-jlcpcb-tools | local JLC DB | text-derived only | ✗ | JLC assembly only |
| jlc-kicad-lib-loader | ✓ | ✗ | ✓ | — |
| kicad-lcsc-manager | ✓ | ✗ | ✓ | — |
| **this** | live JLC API + local DB | **real attributes** | ✓ | **assembly *and* retail** |

## The stock problem this solves

**JLCPCB assembly stock and LCSC retail stock are different inventories.**
They routinely disagree, and picking a part on the wrong number wastes a
board spin. Measured on this project's own BOM (2026-08-02):

| LCSC | JLC assembly | LCSC retail | Meaning |
|---|---|---|---|
| C427451 (AD7124-8BBCPZ) | 0 | 0 | dead everywhere |
| C17168 | 200,900 | 0 | assemblable, but no loose spares for rework |
| C15849 | 14,256,016 | 1,500 | ~9,500× divergence |
| C374726 | 13,418 | 13,415 | agree |

Every part view shows **both** numbers plus a plain-language warning:

- `UNAVAILABLE` — zero on both sides
- `Assembly-blocked` — retail has stock, JLC cannot place it
- `Assembly-only` — JLC can place it, you cannot buy spares
- divergence factor when the two disagree by more than 25%
- JLC minimum purchase quantity and per-order attrition ("loss") count
- Extended-part feeder-fee reminder

Stock is cached for 5 minutes; **Refresh** clears the cache.

### Telling the two apart at a glance

**JLC assembly** and **LCSC retail** are separate inventories whose stock
routinely disagrees, and each gets its own column, colour-coded by how healthy
the figure is (green in stock, amber under 100 pieces, red at zero, grey when
we have not been told). A `…` means the figure is still being fetched — never
confused with a confirmed zero.

The **Inventory** switch at the top picks *which one* the window reports on —
one at a time, not both. It hides the other column and its detail card, and
re-labels the *in stock only* filter so it always says which warehouse it is
filtering on. Sorting by either stock figure is in the **Sort** dropdown.

This is a deliberate limit rather than a missing feature. The keyword search
returns JLC assembly stock for a whole page in one request, but retail stock is
one request *per part* — so reporting both meant a hundred extra lookups per
search, re-fired on every filter change. `wmsc.lcsc.com` answers those with a
403 outright in some regions, and the EasyEDA fallback rate-limits a burst that
size and then refuses your address for minutes, which left the retail column
full of `?`. Choosing **JLC assembly** now issues no retail requests at all;
choosing **LCSC retail** fills the column in the background, two requests at a
time, and the status line counts what is still loading. If both retail hosts
refuse, the status line says so instead of showing the column as empty stock.

Search results use catalogue-style rows: a large product photo, model and
LCSC/library identity, two-line description and category, manufacturer and
package, the chosen inventory's stock, and unit price/minimum order. Selected-part
details can open either as a **Side panel** or as a full-width **Inline below**
expanded row placed directly beneath the selected part, like JLCPCB's parts
library. The choice is saved.

## Parametric filters

The JLC parts-library search API returns real per-part attributes
(`Resistance`, `Tolerance`, `Temperature Coefficient`, `Power`, …). The
Explorer harvests them across a result set, builds a dropdown per attribute
that actually discriminates, and filters on them — LCSC's filter sidebar,
rebuilt inside KiCad.

Filters apply to the **fetched** result set (up to 100 parts per search), not
to all of LCSC. Narrow the keyword to pull a different slice.

> LCSC's own `wmsc.lcsc.com` *bulk search* endpoint returns HTTP 403 to
> anonymous clients, so bulk filtering uses the JLC parts library. The LCSC
> *detail* endpoint does work anonymously and supplies the retail stock,
> warehouse split, price ladder and parameters shown for a selected part.

## Compatibility

| | Supported | Notes |
|---|---|---|
| **OS** | macOS, Windows, Linux | installer handles all three |
| **KiCad** | 7, 8, 9, 10 | only `GetBoard().GetFileName()` is used from the `pcbnew` API |
| **Python** | 3.9 – 3.14 | 3.9 is the floor KiCad bundles; verified on 3.9.6, 3.9.13 and 3.14.5 |
| **wxPython** | 4.1+ | previews need `wx.svg`; without it everything else still works |
| **Dependencies** | none to install | uses `wx` + stdlib, `certifi` if present, and the vendored zero-dependency easyeda2kicad |

Deliberate portability choices:

- **Paths** are compared with `normcase`/`abspath`, so `C:\Users` vs `c:/users`
  resolves correctly on Windows.
- **Plugin directory** is discovered per-platform — `~/Documents/KiCad/<ver>`
  on macOS/Windows, `$XDG_DATA_HOME/kicad/<ver>` on Linux.
- **TLS trust** falls back through `LCSC_CA_BUNDLE` / `SSL_CERT_FILE` /
  `REQUESTS_CA_BUNDLE` → certifi → the interpreter default → common distro CA
  bundles. Verification is never disabled; if no trust anchors exist, requests
  fail loudly. (KiCad's macOS Python has no system trust store, which is why
  this chain exists.)
- **Annotations** use `Optional[X]`, not `X | None`, per upstream's
  `CLAUDE.md`. `UP035`/`UP045` are pinned off in `pyproject.toml` so ruff
  cannot rewrite them into 3.10-only syntax.

### Verifying an install

`./install.sh --list` reports where each half is linked and whether the target
is ours. To check the wx plugin imports under **the same interpreter KiCad
uses**:

```bash
# macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
  -c "import kicad_lcsc_suite.lcsc.api as a; print('ok', a.__file__)"
# Windows
& "C:\Program Files\KiCad\10.0\bin\python.exe" -c "import kicad_lcsc_suite.lcsc.api; print('ok')"
```

If stock columns read `?`, the cause is almost always TLS trust or a blocked
host rather than the install — see Troubleshooting below.

## Installing

### 1. Clone

Clone anywhere and **keep the clone** — the installer *links* to it rather
than copying, so this checkout is the live plugin.

```bash
git clone https://github.com/Hung-Chi970104/kicad-lcsc-suite.git
cd kicad-lcsc-suite
```

### 2. Run the installer

macOS / Linux:

```bash
./install.sh                 # newest KiCad found
./install.sh 10.0            # a specific KiCad version
./install.sh --list          # show what it detected, change nothing
./install.sh --dir <path>    # explicit plugin directory
./install.sh --uninstall
```

Windows (PowerShell, **no admin needed** — it creates a directory junction,
not a symlink):

```powershell
.\install.ps1
.\install.ps1 -Version 10.0
.\install.ps1 -Uninstall
```

If PowerShell blocks the script, allow it for that session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

The installer locates KiCad's plugin directory itself
(`~/Documents/KiCad/<ver>/scripting/plugins` on macOS and Windows,
`$XDG_DATA_HOME/kicad/<ver>/scripting/plugins` on Linux) and links this
checkout in as `kicad_lcsc_suite`. Because it links rather than copies,
**`git pull` updates the installed plugin on every machine — no reinstall.**

### 3. Restart KiCad

Then: **PCB editor → Tools → External Plugins → LCSC Suite**.

Named "LCSC Suite" so it can sit alongside an upstream "JLCPCB Tools"
install without producing two identical toolbar entries.

### 4. Confirm it works (optional)

```bash
./install.sh --list     # where each half is linked, and whether it is ours
```

### Manual install (no git)

Copy the **`kicad_lcsc_suite/` directory** (not the repository root) into
KiCad's `scripting/plugins` folder, keeping its name, then restart KiCad. The
name has to stay a valid Python identifier — underscores, not hyphens — because
it is what KiCad imports the package as.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Plugin missing from the menu | Directory name contains hyphens, or KiCad was not restarted |
| Previews blank, rest works | wxPython built without `wx.svg` |
| Stock shows `?` | No CA trust store — set `LCSC_CA_BUNDLE` to a CA bundle |
| Retail column stuck on `…` | LCSC detail endpoint unreachable or rate-limiting; **Refresh** to retry |
| No product photo | Not every part has one; photos are best-effort and never block the rest |
| Imported library not in the symbol chooser | KiCad caches lib-tables at startup; restart it |
| Explorer window opens empty | Should now report the error instead — check the main window's log panel and file it |

## Using it

**LCSC Explorer** (magnifier icon, or seeded from the current footprint
selection):

1. Type a keyword (`22k 0805 0.1%`), an MPN, or an LCSC id (`C374726`).
2. Pick an **Inventory** — JLC assembly or LCSC retail, whichever you are
   ordering from. Narrow with the parametric dropdowns, tick **in stock only**
   to drop unbuyable parts, and **Sort** by stock or price.
3. Select a row — the availability cards fill first, then the symbol and
   footprint drawings, then the product photo. Nothing waits on the photo.
4. **Import symbol + footprint + 3D** writes the part into your library;
   **Assign LCSC number** tags the selected footprints; **Import + assign**
   does both.

**Import libs** (toolbar) batch-imports symbol/footprint/3D for *every* LCSC
number already assigned on the board — useful when picking the project up on
the other machine.

**Import symbol/fp** in the original part selector does the same for one part.

## Board ↔ schematic, in whichever direction you ask for

Assigning tags the **footprint**. On its own that leaves the Symbol Fields
Table empty, the schematic BOM without part numbers, and the assignment at the
mercy of the next *Update PCB from Schematic*, which pushes the symbol's empty
LCSC field over it. The reverse happens just as often: a schematic that
already has every LCSC field filled in, opened here, looks completely
unassigned because nothing ever put those numbers on the footprints.

Two toolbar buttons, and **nothing happens without pressing one**:

| Button | Direction | What it touches |
|---|---|---|
| **From schematic** | symbols → footprints | the board in memory (save the PCB afterwards) |
| **To schematic** | footprints → symbols | the project's `.kicad_sch` files |

Both show you exactly what they are about to overwrite — reference by
reference, old number → new number — and do nothing until you confirm. They
are also genuinely destructive in the direction you pick: the two sides are
never merged, the one you choose wins.

What each deliberately will not do:

- **Touch a schematic that is open in the Schematic Editor.** The editor holds
  its own copy in memory and would overwrite anything written underneath it,
  so *To schematic* says so and stops. Close the Schematic Editor and the
  numbers go in — reopen it and the Symbol Fields Table has them. *From
  schematic* still works while it is open, but reads the file on disk, so it
  warns that unsaved edits are not included.
- **Clear a number it was not asked to clear.** Going out, only parts you
  explicitly removed are blanked; a symbol carrying a number the board has not
  caught up with keeps it. Coming in, a symbol with no number leaves the
  footprint alone.
- **Rewrite a file with nothing to change.** Sheets that already match are left
  untouched; a sheet that is rewritten leaves the previous version beside it as
  `<name>.kicad_sch_old`.

Hierarchical sheets are followed in both directions, from the root sheet down
through every `Sheetfile`.

## Where imported parts land

A library triplet, project-local by default so the design stays portable:

```
<board dir>/lcsc-lib/LCSC.kicad_sym      symbols
<board dir>/lcsc-lib/LCSC.pretty/        footprints
<board dir>/lcsc-lib/LCSC.3dshapes/      3D models (.wrl + .step)
```

The plugin registers this in the project's `sym-lib-table` and `fp-lib-table`
using `${KIPRJMOD}`, backing up any table it modifies to
`*.lcsc-suite.bak`. Point **Import into:** outside the project directory and
it registers globally with an absolute path instead.

**KiCad caches library tables at startup** — restart KiCad if a freshly
imported library does not show up in the symbol chooser.

## Limitations

- Not every LCSC part has an EasyEDA drawing; those import as "no CAD data".
  Nothing is fabricated to fill the gap.
- Converted footprints are only as good as EasyEDA's source data. **Check the
  footprint against the datasheet before committing to a board** — this is a
  convenience importer, not a verified library.
- Bulk parametric filtering covers the fetched page, not all of LCSC.
- Schematic sync is text surgery on `.kicad_sch` files, not an API call —
  KiCad's IPC API does not reach the schematic. It writes nothing while the
  Schematic Editor has the file open.
- Stock and price come from unofficial endpoints that can change or rate-limit
  without notice. Failures degrade to "?" rather than crashing.

## Layout

The UI is being rewritten out-of-process in PySide6 (see
[docs/QT_MIGRATION_PLAN.md](docs/QT_MIGRATION_PLAN.md)), so there are two
application packages side by side. They do not collide — each is a separate
entry point, and `./install.sh --list` shows both.

```text
kicad_lcsc_suite/      the wx plugin KiCad loads, on its bundled Python 3.9
  lcsc/api.py            JLC assembly + LCSC retail + JLC search; StockReport
  lcsc/importer.py       EasyEDA -> KiCad library; lib-table registration
  lcsc/explorer.py       the LCSC Explorer dialog
  lcsc/previewpanel.py   symbol/footprint (wx.svg) and photo preview tiles
  lcsc/theme.py          light/dark aware status and inventory colours
  store.py library.py    the SQLite layers, shared with the Qt app
  icons/                 the icon set, shared with the Qt app
  lib/easyeda2kicad/     vendored, zero-dependency converter

lcsc_suite/            the new PySide6 app, own venv, Python 3.12+
  kicad_bridge.py        the KiCad 10 IPC API, with its traps wrapped
  shared.py              the only sanctioned way in to the modules above
  ui/                    Qt widgets, Fusion style

kicad_plugin/          IPC manifest + launcher for the Qt app
db_build/              the parts-database GitHub Action, and its common/ library
scripts/               gui_probe.py (wx) and qt_probe.py (Qt) screenshot harnesses
tests/                 every test, for both halves
install.sh / .ps1      symlink/junction installer
UPSTREAM.txt           pinned upstream commits
```

The wx half is Python 3.9 (what KiCad bundles) and uses only `wx`, `certifi`
and the standard library; the Qt half brings its own interpreter and PySide6.
`AGENTS.md` and `CLAUDE.md` carry the contributor rules for both.

## Licensing — read before making this public

This repository combines two upstreams under **different** licenses:

| Component | Upstream | License |
|---|---|---|
| Plugin base (`kicad_lcsc_suite/mainwindow.py`, `fabrication.py`, `store.py`, …) | [Bouni/kicad-jlcpcb-tools][bouni] | MIT (`LICENSE`) |
| `kicad_lcsc_suite/lib/easyeda2kicad/` (vendored) | [uPesy/easyeda2kicad.py][e2k] | **AGPL-3.0** (its own `LICENSE`) |
| `kicad_lcsc_suite/lcsc/`, `lcsc_suite/`, installers | this repo | see below |

The AGPL is the binding constraint. **While this repository stays private
and you only run the plugin yourself, nothing is triggered** — the AGPL's
obligations attach to *distribution* and to *network-service* use, neither
of which applies to personal use of a private checkout.

If you ever make this repo public, publish a release, or let others use it
over a network, the combined work must be offered under **AGPL-3.0**, with
complete corresponding source. Two ways to stay clean:

1. **Relicense the whole thing AGPL-3.0** — simplest, and compatible, since
   MIT code can be included in an AGPL work.
2. **Un-vendor easyeda2kicad** — drop `kicad_lcsc_suite/lib/easyeda2kicad/`,
   make it a pip dependency the user installs, and keep this repo MIT. Removes
   the "no dependencies to install" property.

Upstream commit pins are in `UPSTREAM.txt`. Both upstream licenses are kept
in the tree; do not delete them.

## Credits

The plugin this forks from is **[kicad-jlcpcb-tools][bouni] by Bouni**, which
is where the BOM/CPL writers, the corrections subsystem, the project database
and the parts-database build pipeline all came from. If this is useful to you,
[the original author takes sponsorship](https://github.com/sponsors/Bouni).

- The EasyEDA→KiCad converter is [uPesy/easyeda2kicad.py][e2k], vendored.
- Footprint rotation corrections originate in
  [matthewlai/JLCKicadTools](https://github.com/matthewlai/JLCKicadTools).
- The icon set is [Material Design Icons](https://materialdesignicons.com/).

Upstream's own README covers features this fork has dropped or replaced —
Gerber and drill output most of all, which is
[deliberately out of scope](docs/QT_MIGRATION_PLAN.md).
