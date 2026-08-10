<#
.SYNOPSIS
    Install kicad-lcsc-suite into KiCad's plugin directories (Windows).

.DESCRIPTION
    One half now. The Phase 8 cutover removed the in-process wxPython plugin
    (docs/QT_MIGRATION_PLAN.md); what installs is the out-of-process PySide6
    application. It bootstraps a virtualenv, pip-installs PySide6 +
    kicad-python, and registers an IPC API plugin in
    <Documents>\KiCad\<ver>\plugins\lcsc_suite so KiCad shows a toolbar button.

    Junctions rather than symlinks: they need no administrator rights.

    -App is still accepted and does nothing, so a note or a script carrying it
    keeps working. -Wx is refused with a message rather than ignored: it used
    to install something, and silently installing nothing is the worse answer.

    NOTE: this adds a one-time setup step (a virtualenv) the wx plugin never
    had. That is a deliberate product decision recorded in the migration plan's
    §8. KiCad 10 or newer is required.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Version 10.0
    .\install.ps1 -Uninstall
    .\install.ps1 -List
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$App,
    [switch]$Wx,
    [switch]$Uninstall,
    [switch]$List
)

$ErrorActionPreference = 'Stop'

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppLinkName = 'lcsc_suite'
# Where the wx plugin used to be junctioned. Kept so -Uninstall can still clean
# up after an install made before the cutover; nothing writes it any more.
$LegacyWxLinkName = 'kicad_lcsc_suite'
$Venv = Join-Path $Src '.venv'
# Pinned rather than floating: the IPC API is young and its wire format has
# changed between KiCad 10.x point releases. easyeda2kicad is pinned exactly —
# its converters decide the on-disk shape of every symbol, footprint and 3D
# model, and it is AGPL-3.0, so pip fetches it rather than this repo shipping
# it. Keep in step with APP_REQUIREMENTS in install.sh.
# requests is not optional: library.py imports it at module scope, and
# shared.py -> kicad_bridge.py pulls that in, so a venv without it cannot start
# the app at all. Dev machines have it via the db_build tooling; a fresh venv
# does not, and easyeda2kicad does not bring it.
$AppRequirements = @(
    'PySide6>=6.7,<7', 'kicad-python>=0.4,<1', 'easyeda2kicad==1.0.1', 'requests>=2.28'
)

if ($Wx) {
    Write-Error 'The wx plugin was removed at the Phase 8 cutover. See docs/QT_MIGRATION_PLAN.md; this installs the Qt app.'
    exit 2
}

$KicadDocs = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'KiCad'
if (-not (Test-Path $KicadDocs)) {
    throw "KiCad user directory not found at $KicadDocs. Open KiCad once, then re-run."
}

if (-not $Version) {
    $Version = Get-ChildItem -Path $KicadDocs -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } |
        Select-Object -Last 1 -ExpandProperty Name
}
if (-not $Version) {
    throw "No versioned KiCad directory (e.g. 10.0) found under $KicadDocs."
}

$KicadDir = Join-Path $KicadDocs $Version
$WxTarget = Join-Path $KicadDir "scripting\plugins\$LegacyWxLinkName"
$AppTarget = Join-Path $KicadDir "plugins\$AppLinkName"

function Describe-Target([string]$Target) {
    if (-not (Test-Path $Target)) { return 'not installed' }
    $item = Get-Item $Target -Force
    if ($item.LinkType) { return "installed -> $($item.Target)" }
    return 'occupied by a non-junction'
}

function Remove-Ours([string]$Target, [switch]$AllowDirectory) {
    if (Test-Path $Target) {
        $item = Get-Item $Target -Force
        if ($item.LinkType) {
            $item.Delete()
            Write-Host "Removed $Target"
        } elseif ($AllowDirectory -and (Test-Path (Join-Path $Target 'plugin.json'))) {
            # The app half is copied rather than junctioned; recognise it by its
            # manifest so an unrelated directory is never deleted.
            Remove-Item $Target -Recurse -Force
            Write-Host "Removed $Target"
        } else {
            Write-Warning "$Target exists but was not installed by this script; left alone."
        }
    } else {
        Write-Host "Nothing installed at $Target"
    }
}

function New-Junction([string]$Target, [string]$Source) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    if (Test-Path $Target) {
        $item = Get-Item $Target -Force
        if ($item.LinkType) {
            $item.Delete()
        } else {
            throw "$Target exists and is not a junction. Move it aside, then re-run."
        }
    }
    New-Item -ItemType Junction -Path $Target -Value $Source | Out-Null
}

if ($List) {
    Write-Host "source     : $Src"
    Write-Host "KiCad dir  : $KicadDir"
    Write-Host "app target : $AppTarget"
    Write-Host "  status   : $(Describe-Target $AppTarget)"
    Write-Host "venv       : $Venv"
    $venvPython = Join-Path $Venv 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        Write-Host "  python   : $(& $venvPython --version 2>&1)"
    } else {
        Write-Host '  python   : absent'
    }
    Write-Host "wx target  : $WxTarget (removed at the cutover)"
    Write-Host "  status   : $(Describe-Target $WxTarget)"
    return
}

if ($Uninstall) {
    Remove-Ours $AppTarget -AllowDirectory
    # Unconditional: an install predating the cutover left a junction here.
    Remove-Ours $WxTarget
    Write-Host ''
    Write-Host "The virtualenv at $Venv was left in place; delete it by hand if"
    Write-Host 'you want it gone.'
    return
}

# --- the new Qt app --------------------------------------------------------

function Find-Python {
    # PySide6 needs >= 3.10 and this app targets 3.12+. KiCad's own bundled
    # Python is 3.9 and cannot run it — that is the whole reason the app runs
    # out of process.
    foreach ($candidate in @('python3.14', 'python3.13', 'python3.12', 'py', 'python')) {
        $exe = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $ok = & $exe.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe.Source }
    }
    throw 'No Python >= 3.12 found. Install one from python.org, then re-run.'
}

function Test-ApiServer {
    # KiCad ships with the IPC API server *off*, and the app cannot connect
    # without it. A clear instruction beats a silent failure to connect.
    $prefs = Join-Path $env:APPDATA "kicad\$Version\kicad_common.json"
    if (-not (Test-Path $prefs)) {
        Write-Host "note: could not find $prefs to check the API server setting."
        Write-Host '      Make sure Preferences -> Plugins -> Enable KiCad API is ticked.'
        return
    }
    $config = Get-Content $prefs -Raw | ConvertFrom-Json
    if ($config.api.enable_server) {
        Write-Host 'KiCad API server : enabled'
    } else {
        Write-Host ''
        Write-Host '!! KiCad''s API server is DISABLED. The EasyAssembly button will not'
        Write-Host '!! be able to reach your board until you enable it:'
        Write-Host '!!'
        Write-Host '!!     KiCad -> Preferences -> Plugins -> Enable KiCad API'
        Write-Host '!!'
        Write-Host "!! ($prefs)"
        Write-Host ''
    }
}

$venvPython = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    $python = Find-Python
    Write-Host "Creating virtualenv at $Venv using $python"
    & $python -m venv $Venv
}
Write-Host 'Installing app dependencies'
& $venvPython -m pip install --upgrade --quiet pip
& $venvPython -m pip install --quiet @AppRequirements

# Copied, not junctioned. The two files below have to be *rewritten* for
# Windows — the manifest's entrypoint, and the checkout path run.cmd cannot
# discover on its own — and writing those through a junction would dirty
# the git checkout. Only the manifest, the launcher and the icons live here;
# the app's Python stays in the checkout and still updates with `git pull`.
if (Test-Path $AppTarget) {
    $existing = Get-Item $AppTarget -Force
    if ($existing.LinkType) { $existing.Delete() }
    else { Remove-Item $AppTarget -Recurse -Force }
}
New-Item -ItemType Directory -Force -Path $AppTarget | Out-Null
Copy-Item -Path (Join-Path $Src 'kicad_plugin\*') -Destination $AppTarget -Recurse -Force

Set-Content -Path (Join-Path $AppTarget 'repo_root.txt') -Value $Src -Encoding ASCII -NoNewline
$manifestPath = Join-Path $AppTarget 'plugin.json'
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$manifest.actions[0].entrypoint = 'run.cmd'
$manifest | ConvertTo-Json -Depth 10 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host 'Installed the EasyAssembly app'
Write-Host "  source : $(Join-Path $Src 'kicad_plugin')"
Write-Host "  target : $AppTarget"
Write-Host "  python : $(& $venvPython --version 2>&1)"
Test-ApiServer

# --- the legacy wx plugin, if one is still linked ---------------------------
#
# Not installed any more — removed. But an install from before the cutover left
# a junction pointing at a directory that no longer exists, and KiCad logs an
# import error for it on every start. Clearing it is the one thing this script
# still has to say about the wx half.

if (Test-Path $WxTarget) {
    Remove-Ours $WxTarget
    Write-Host 'Removed the old wx plugin link'
    Write-Host "  target : $WxTarget"
}

Write-Host ''
Write-Host 'Restart KiCad. In the PCB editor you will find:'
Write-Host "  the 'EasyAssembly' toolbar button"
