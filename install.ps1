<#
.SYNOPSIS
    Install kicad-lcsc-suite into KiCad's plugin directories (Windows).

.DESCRIPTION
    Two halves, because a migration is underway (docs/QT_MIGRATION_PLAN.md):

      App  the new out-of-process PySide6 application. Bootstraps a virtualenv,
           pip-installs PySide6 + kicad-python, and registers an IPC API plugin
           in  <Documents>\KiCad\<ver>\plugins\lcsc_suite  so KiCad shows a
           toolbar button.
      Wx   the legacy in-process wxPython plugin. Junctions the checkout into
           <Documents>\KiCad\<ver>\scripting\plugins\kicad_lcsc_suite. Removed
           at the migration's Phase 8 cutover; until then both can be installed
           and they do not collide.

    Junctions rather than symlinks: they need no administrator rights.

    NOTE: the app half adds a one-time setup step (a virtualenv) the wx plugin
    never had. That is a deliberate product decision recorded in the migration
    plan's §8.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -App
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
# Must be a valid Python identifier — the repo directory name has hyphens.
$WxLinkName = 'kicad_lcsc_suite'
$AppLinkName = 'lcsc_suite'
$Venv = Join-Path $Src '.venv'
# Pinned rather than floating: the IPC API is young and its wire format has
# changed between KiCad 10.x point releases.
$AppRequirements = @('PySide6>=6.7,<7', 'kicad-python>=0.4,<1')

# Neither switch given means both halves.
$DoApp = $App -or (-not $App -and -not $Wx)
$DoWx = $Wx -or (-not $App -and -not $Wx)

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
$WxTarget = Join-Path $KicadDir "scripting\plugins\$WxLinkName"
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
    Write-Host "wx target  : $WxTarget"
    Write-Host "  status   : $(Describe-Target $WxTarget)"
    return
}

if ($Uninstall) {
    if ($DoApp) { Remove-Ours $AppTarget -AllowDirectory }
    if ($DoWx) { Remove-Ours $WxTarget }
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
        Write-Host '!! KiCad''s API server is DISABLED. The LCSC Suite button will not'
        Write-Host '!! be able to reach your board until you enable it:'
        Write-Host '!!'
        Write-Host '!!     KiCad -> Preferences -> Plugins -> Enable KiCad API'
        Write-Host '!!'
        Write-Host "!! ($prefs)"
        Write-Host ''
    }
}

if ($DoApp) {
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

    Write-Host 'Installed the LCSC Suite app'
    Write-Host "  source : $(Join-Path $Src 'kicad_plugin')"
    Write-Host "  target : $AppTarget"
    Write-Host "  python : $(& $venvPython --version 2>&1)"
    Test-ApiServer
}

# --- the legacy wx plugin --------------------------------------------------

if ($DoWx) {
    New-Junction $WxTarget $Src
    Write-Host 'Installed the legacy wx plugin'
    Write-Host "  source : $Src"
    Write-Host "  target : $WxTarget"
}

Write-Host ''
Write-Host 'Restart KiCad. In the PCB editor you will find:'
if ($DoApp) { Write-Host "  the 'LCSC Suite' toolbar button (the new Qt app)" }
if ($DoWx) { Write-Host '  Tools -> External Plugins -> LCSC Suite (the legacy wx plugin)' }
