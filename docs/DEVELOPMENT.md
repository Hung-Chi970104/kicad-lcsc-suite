# Development

How to set up, test, lint, run and debug this plugin. Companion to
[../AGENTS.md](../AGENTS.md) (rules and navigation) and
[ARCHITECTURE.md](ARCHITECTURE.md) (how the code fits together).

Every command below was run against this checkout; the noted baselines are
what you should actually see.

---

## 1. One interpreter

The Phase 8 cutover removed the in-process wx plugin, and with it the two-
interpreter split that used to trip people up. Everything now runs in **one
virtualenv on Python 3.12+**: the app, `pytest`, `ruff`, the probes and the
`db_build` tooling.

```bash
./install.sh            # creates .venv, installs PySide6 + kicad-python
.venv/bin/python -m pytest -q
```

KiCad's own bundled Python (3.9) is no longer used for anything here. That is
the whole point of running out of process: PySide6 needs ≥3.10, KiCad 10 still
bundles 3.9.13, and leaving its interpreter is what lifted the constraint.

The rules that came with it are gone too — no `Optional[X]`-over-`X | None`, no
avoiding `match`/`case`. `pyproject.toml` still pins `UP006/UP007/UP035/UP045`
off; turning them back on is a deliberate change, not a drive-by.


## 2. Setup

Neither `pytest` nor `ruff` is preinstalled on this machine.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # pulls cachetools, requests, tqdm, … + pytest + ruff
.venv/bin/pip install "ruff==0.14.14"  # match the pre-commit pin — see §4
```

`.venv/` is gitignored. The editable install is only for the test suite's
sake: the *plugin* still has no installable dependencies, and adding one is
a rule violation ([AGENTS.md](../AGENTS.md#hard-rules)).

## 3. Tests

```bash
.venv/bin/python -m pytest          # 512 passed in ~7s
.venv/bin/python -m pytest -q tests/test_bom_estimator.py
.venv/bin/python -m pytest -q -k "stock or retail"
```

`testpaths` is `["tests"]` (pyproject.toml) — one directory.
[tests/conftest.py](../tests/conftest.py) puts the repository root on
`sys.path`, which is all a test needs to `import lcsc_suite` or `db_build` by
name.

**Every test file also passes on its own**, and that is worth preserving.
Several import a wx-dependent module under a `MagicMock` toolkit, and because
the modules are now shared rather than loaded per-file under a synthetic
package name, one file's stub is visible to the next. Two rules keep that
harmless: install stubs with `sys.modules.setdefault`, never by assignment;
and if a test needs a *specific* value out of a stub, pin it on the imported
module (`monkeypatch.setattr`, or an autouse fixture) rather than racing on
who imports first. Check with:

```bash
for f in tests/test_*.py; do .venv/bin/python -m pytest -q "$f" >/dev/null || echo "FAILS ALONE: $f"; done
```

Everything under test is deliberately wx-free:
`lcsc_suite/bom_estimation/pricing.py`, `db_build/common/translate.py`,
`fabrication.split_bom_designators` and friends. `events.py` dispatches to a Qt
sink or `wx.PostEvent` depending on the destination, which is what keeps the
importing modules testable — and what lets `library.py` serve both halves.

**There is no automated coverage of the wx dialogs.** That is what §5 is for.

## 4. Lint

```bash
.venv/bin/ruff check --extend-exclude=lib
.venv/bin/ruff format --check --exclude lib
```

Three things you need to know:

**Always pass the `lib` exclusion.** Plain `ruff check` walks the vendored
`lib/` and reports **3445 errors**. `.pre-commit-config.yaml` passes
`--extend-exclude=lib` / `exclude: '(^|/)lib'`; do the same by hand or you
will drown in noise from code you must not touch.

**Pin ruff to 0.14.14**, the version in `.pre-commit-config.yaml`. Newer
ruff (0.16.x) adds `PLR0917` and flags two pre-existing files
(`tests/test_componentdb.py`, `db_build/jlcparts_db_convert.py`), plus it
reformats differently. Chasing those is churn.

**`ruff format --check` is not clean at HEAD.** Four untouched upstream
files would be reformatted: `corrections.py`, `events.py`, `kicad_drc.py`,
`partdetails.py`. **Leave them alone.** `CLAUDE.md`'s "only reformat lines
you are intentionally changing" exists precisely to keep the fork's diff
against upstream readable. `ruff check` *does* pass clean at HEAD — that is
the gate to keep green.

Pre-commit is configured (ruff, pyupgrade, markdownlint) if you want it:

```bash
.venv/bin/pip install pre-commit && .venv/bin/pre-commit run --all-files
```

## 5. Verifying GUI changes headlessly

**The rule the whole migration rests on: a claim about the UI is not made until
a screenshot has been looked at.** Geometry dumps miss what users see.

`scripts/qt_probe.py` builds any screen offscreen and grabs it to a PNG. No
display, no window manager, no screen-recording permission — it works over SSH
and in CI:

```bash
.venv/bin/python scripts/qt_probe.py --list             # every screen name
.venv/bin/python scripts/qt_probe.py mainwindow         # one, light
.venv/bin/python scripts/qt_probe.py --all --theme both # all of them, both appearances
.venv/bin/python scripts/qt_probe.py explorer --geometry
```

Screens render against `lcsc_suite/fixtures/board.json` (110 footprints) and a
captured LCSC search, so a run is reproducible and never touches the network.
`--live` connects to a running KiCad instead, for when the question is about
real data.

Commit the updated PNGs **in the same commit** as the UI change, so the diff
shows the visual change. Adding a screen is a `screen_*` builder plus an entry
in `SCREENS`; CI covers it automatically.

### The cross-platform gate

Fusion draws its own widgets, so layout is meant to be identical on macOS,
Windows and Linux. That is a claim, and it is checked:

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both --geometry-out mine.txt
.venv/bin/python scripts/compare_geometry.py docs/screens/geometry.txt mine.txt
```

`docs/screens/geometry.txt` is the committed macOS reference — 3114 lines over
19 screens in both appearances. The `windows` job in
`.github/workflows/qt-screens.yml` renders on `windows-latest` and runs exactly
that comparison.

It gates on three things and reports everything else:

- **the widget tree** — every widget, its nesting, its text and its hidden flag.
  This is what catches the failure that matters: a label the platform had to
  elide changes its *text*, and a toolbar that overflows shows its extension
  arrow;
- **the size of every window that states one** with `resize()`. The two dialogs
  that size themselves to their contents get a budget instead;
- **collapse** — a widget with a size on one platform and none on the other.

It deliberately does **not** gate on pixel equality. The app pins a font *size*
but not a font *family*, so text is Segoe UI on Windows and the system face on
macOS; 2132 widgets differ in size and the largest differences are spacers doing
their job. `scripts/compare_geometry.py`'s docstring carries the measurements.

### Board writes

A screenshot says nothing about whether the board changed. After touching
`kicad_bridge.py`, open a **copy** of a board in KiCad and run:

```bash
mkdir -p /tmp/lcsc-live
cp <some board>.kicad_pcb /tmp/lcsc-live/livecheck.kicad_pcb
open -a "/Applications/KiCad/PCB Editor.app" /tmp/lcsc-live/livecheck.kicad_pcb
.venv/bin/python scripts/live_ipc_check.py     # refuses non-disposable boards
```

Close the board **without saving** afterwards. This is how Phase 3 found trap 4,
after two phases of green fixture tests.


## 6. Running the real thing

**In KiCad.** `install.sh` symlinks the checkout in, so there is no
reinstall step — edit, restart KiCad, then
**PCB editor → Tools → External Plugins → LCSC Suite**.

```bash
./install.sh              # newest KiCad found
./install.sh --list       # show detection, change nothing
./install.sh --uninstall
```

KiCad caches the plugin module *and* library tables at startup, so a restart
is needed for both code changes and freshly imported libraries.

**Standalone**, with no KiCad running at all, against the committed board
fixture:

```bash
.venv/bin/python -m lcsc_suite --fixture
```

The fixture is a real 110-footprint board serialised to JSON, and
`FixtureBoard` reproduces the two IPC traps that bite writes — trap 2 with
`honour_footprint_writes=False`, and trap 4 by staging writes in `_pending`
until `_push`. That second one was added *after* trap 4 escaped two phases of
green tests, which is the rule this fixture now embodies: a fixture is only
evidence to the extent it is **less** permissive than the thing it stands in
for.

## 7. Environment diagnostics

`selfcheck.py` was deleted — most of what it checked was fabrication
readiness, which is now out of scope (see the migration plan's §1). Reachability
is the part worth keeping, and it is one command:

```bash
curl -o /dev/null -w '%{http_code}\n' \
  'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C1592'
```

A 403 there is normal and expected — LCSC refuses whole networks, which is
exactly why retail stock falls back to EasyEDA and photos come from JLC's file
service. JLCPCB's own endpoints are unaffected.

## 8. Databases while developing

| Want | Do |
|---|---|
| Drop the optional bulk parts DB | `rm jlcpcb/*parts*.db` — no re-download follows; use the Download button |
| Force the API part cache to refetch | `rm jlcpcb/partcache.db`, or `update part_cache set fetched_at = 0` |
| Inspect what the API resolved for a part | `sqlite3 jlcpcb/partcache.db 'select * from part_cache where lcsc = "C15849"'` |
| Reset one board's plugin state | delete `<board dir>/jlcpcb/project.db` |
| Inspect assignments | `sqlite3 <board dir>/jlcpcb/project.db 'select * from part_info'` |
| Reset plugin settings | delete `settings.json` at the repo root |
| Undo a lib-table edit | restore the `*.lcsc-suite.bak` the importer wrote |

Adding a per-part field? Extend `Store.PART_INFO_ESTIMATOR_COLUMNS` —
`ensure_part_info_columns` `ALTER TABLE`s it in on open. Never write a
destructive migration; these files live in users' board directories.

## 9. Debugging

Logging goes to the main window's log panel via `LogBoxHandler`
([mainwindow.py:2123](../mainwindow.py#L2123)), so `logging.getLogger(__name__)`
output is visible in the UI. Every module already has a `self.logger`.

Common failure signatures:

| Symptom | Look at |
|---|---|
| Dialog opens blank or half-built | a `wxAssertionError` aborted `_build_ui()` — check sizer alignment flags against orientation |
| `RuntimeError` deep in the wx event loop | a `wx.CallAfter` landed on a destroyed window — missing `_alive()` guard |
| Stale results overwrite fresh ones | missing staleness-token check (`_search_token` / `_detail_token` / `_retail_token` / `assembly_enrichment_generation`) |
| Stock shows `?` | TLS trust — set `LCSC_CA_BUNDLE` to a CA bundle |
| Retail column stuck on `…` | LCSC detail endpoint unreachable or rate-limiting; **Refresh** retries |
| Plugin missing from the menu | directory name has hyphens, or KiCad was not restarted |
| Import not in the symbol chooser | KiCad caches lib-tables at startup — restart |
| Column widths collapse | widths set before the window was shown; restate in `_on_first_shown` |

## 10. Before you commit

```bash
.venv/bin/ruff check --extend-exclude=lib          # must pass clean
.venv/bin/ruff format --check --exclude lib        # must pass clean too, since Phase 8
.venv/bin/python -m pytest -q                      # 770 passed
```

Plus, if you touched the UI — and look at the images, do not just run it:

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both
```

Commit the changed PNGs in the same commit. If you touched `kicad_bridge.py`,
run `scripts/live_ipc_check.py` against a copy of a board as well.
