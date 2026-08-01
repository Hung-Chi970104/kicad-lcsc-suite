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

Run the self-check with the **same interpreter KiCad uses**:

```bash
# macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 selfcheck.py
# Windows
& "C:\Program Files\KiCad\10.0\bin\python.exe" selfcheck.py
# Linux
python3 selfcheck.py
```

It reports interpreter version, wx/wx.svg/wx.dataview, `pcbnew`, TLS trust,
the vendored converter, and live reachability of both storefronts. Add
`--offline` to skip the network probes. Exit code is non-zero if something
the plugin needs is missing.

## Installing

macOS / Linux:

```bash
cd ~/Research/kicad-lcsc-suite
./install.sh                 # newest KiCad found
./install.sh 10.0            # a specific version
./install.sh --uninstall
```

Windows (PowerShell, no admin needed — it makes a junction):

```powershell
cd $HOME\Research\kicad-lcsc-suite
.\install.ps1
.\install.ps1 -Version 10.0
.\install.ps1 -Uninstall
```

Both link rather than copy, so `git pull` updates the installed plugin on
either machine with no reinstall.

Restart KiCad, then: **PCB editor → Tools → External Plugins → LCSC Suite**.
It is named "LCSC Suite" so it can coexist with an upstream "JLCPCB Tools"
install.

## Using it

**LCSC Explorer** (magnifier icon, or seeded from the current footprint
selection):

1. Type a keyword (`22k 0805 0.1%`), an MPN, or an LCSC id (`C374726`).
2. Narrow with the parametric dropdowns; tick **JLC stock > 0** to hide
   unbuyable parts.
3. Select a row — symbol and footprint previews render, both stock figures
   and all warnings appear.
4. **Import symbol + footprint + 3D** writes the part into your library;
   **Assign LCSC number** tags the selected footprints; **Import + assign**
   does both.

**Import libs** (toolbar) batch-imports symbol/footprint/3D for *every* LCSC
number already assigned on the board — useful when picking the project up on
the other machine.

**Import symbol/fp** in the original part selector does the same for one part.

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
- Stock and price come from unofficial endpoints that can change or rate-limit
  without notice. Failures degrade to "?" rather than crashing.

## Layout

```
selfcheck.py           environment diagnostic
lcsc/api.py            JLC assembly + LCSC retail + JLC search; StockReport
lcsc/importer.py       EasyEDA -> KiCad library; lib-table registration
lcsc/explorer.py       the LCSC Explorer dialog
lcsc/previewpanel.py   wx.svg-backed SVG preview widget
lib/easyeda2kicad/     vendored, zero-dependency converter
install.sh / .ps1      symlink/junction installer
UPSTREAM.txt           pinned upstream commits
```

Everything is Python 3.9 (what KiCad bundles) and uses only `wx`, `certifi`
and the standard library. `CLAUDE.md` in this directory is upstream's own
contributor guidance and still applies to the forked files.
