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

`.venv/` is gitignored. `./install.sh` builds the same venv and adds the app's
own runtime dependencies (PySide6, kicad-python) — which the app *does* have, and
which are declared deliberately; see hard rule 4 in
[AGENTS.md](../AGENTS.md#hard-rules). The "no installable dependencies" rule this
section used to cite belonged to KiCad's bundled interpreter and went with it at
the Phase 8 cutover.

## 3. Tests

```bash
.venv/bin/python -m pytest          # 789 passed in ~90s
.venv/bin/python -m pytest -q tests/test_bom_estimator.py
.venv/bin/python -m pytest -q -k "stock or retail"
```

`testpaths` is `["tests"]` (pyproject.toml) — one directory.
[tests/conftest.py](../tests/conftest.py) puts the repository root on
`sys.path`, which is all a test needs to `import lcsc_suite` or `db_build` by
name.

**Every test file also passes on its own**, and that is worth preserving.
Modules are shared rather than loaded per-file under a synthetic package name,
so one file's stub is visible to the next. Two rules keep that harmless: install
stubs with `sys.modules.setdefault`, never by assignment; and if a test needs a
*specific* value out of a stub, pin it on the imported module
(`monkeypatch.setattr`, or an autouse fixture) rather than racing on who imports
first. Check with:

```bash
for f in tests/test_*.py; do .venv/bin/python -m pytest -q "$f" >/dev/null || echo "FAILS ALONE: $f"; done
```

The suite builds real Qt widgets — there is no toolkit to stub out any more — and
still never touches the network: it passes `FixtureSource` and
`Library(allow_network=False)`.

**What the suite cannot cover is what §5 and §6 are for**: whether a screen
*looks* right, and whether a write actually reaches the board.

## 4. Lint

```bash
.venv/bin/ruff check --extend-exclude=lib
.venv/bin/ruff format --check --exclude lib
```

Three things you need to know:

**Always pass the `lib` exclusion.** Plain `ruff check` walks the vendored
`lib/packaging/` and reports **3160 errors**. `.pre-commit-config.yaml` passes
`--extend-exclude=lib` / `exclude: '(^|/)lib'`; do the same by hand or you
will drown in noise from code you must not touch.

**Pin ruff to 0.14.14**, the version in `.pre-commit-config.yaml`. Newer
ruff (0.16.x) adds `PLR0917` and flags two pre-existing files
(`tests/test_componentdb.py`, `db_build/jlcparts_db_convert.py`), plus it
reformats differently. Chasing those is churn.

**Both commands are clean at HEAD, and CI runs both.** This used to say that
`ruff format --check` was expected to fail on four untouched upstream files
(`corrections.py`, `events.py`, `kicad_drc.py`, `partdetails.py`) and that you
should leave them alone. All four went with the wx plugin at the Phase 8 cutover,
so if `ruff format --check` reports anything now, it is yours. `CLAUDE.md`'s
"only reformat lines you are intentionally changing" still stands, for the same
reason it always did: keeping the fork's diff against upstream readable.

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

**A run with no UI change leaves the tree clean.** Re-render twice and the 38
PNGs are byte-identical, so `git status` after a probe run lists exactly the
screens your change altered and nothing else. That is worth protecting: it used
to list 21 either way, because the log pane's clock, the throwaway project's
`mkdtemp` path and a half-finished fade animation all landed in the pixels, and a
diff that is noisy in the same places every time is a diff nobody reads. If a
screen you did not touch starts moving, something has reintroduced a dependency
on *when* or *where* the probe ran — see `freeze_log_clock`,
`PROBE_PROJECT_ROOT` and the double grab in `render`.

`geometry.txt` is the one exception, and only for two lines: one `[hidden]`
scroll container in `explorer-reopened-dark` flips between 1434px and 1448px
between runs. Nobody can see it and the gate skips hidden widgets' sizes on
purpose — `compare_geometry.py` names this exact widget in its own comment — so
a four-line diff there is noise, not a finding.

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
**PCB editor → Tools → External Plugins → EasyAssembly**.

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
.venv/bin/python -m lcsc_suite --fixture lcsc_suite/fixtures/board.json
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
| Undo a lib-table edit | restore the `*.easyassembly.bak` the importer wrote (`*.lcsc-suite.bak` if it predates the rebrand) |

Adding a per-part field? Extend `Store.PART_INFO_ESTIMATOR_COLUMNS` —
`ensure_part_info_columns` `ALTER TABLE`s it in on open. Never write a
destructive migration; these files live in users' board directories.

## 9. Debugging

Logging goes to the main window's log panel via `LogHandler`
([lcsc_suite/ui/log_pane.py](../lcsc_suite/ui/log_pane.py)), installed on the
root logger, so `logging.getLogger(__name__)` output from any module — including
worker threads — is visible in the UI.

**A crash before the window exists is invisible in KiCad**, which discards both
streams of an `exec` plugin. `kicad_plugin/run.sh` redirects them to
`~/.local/state/easyassembly/plugin.log`; that file is the only place a start-up
traceback can be read.

Common failure signatures:

| Symptom | Look at |
|---|---|
| Toolbar button does nothing at all | `~/.local/state/easyassembly/plugin.log`. Usually trap 1: a `PYTHONHOME` that `run.sh` did not clear, killing the venv Python before it runs a line |
| A write returns success and the board is unchanged | trap 2 — the write must target the parent footprint, not the field; or trap 4 — you re-read between `begin_commit()` and `push_commit()`, where an open commit is invisible |
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
.venv/bin/python -m pytest -q                      # 789 passed
```

Plus, if you touched the UI — and look at the images, do not just run it:

```bash
.venv/bin/python scripts/qt_probe.py --all --theme both
```

Commit the changed PNGs in the same commit. If you touched `kicad_bridge.py`,
run `scripts/live_ipc_check.py` against a copy of a board as well.
