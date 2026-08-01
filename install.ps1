<#
.SYNOPSIS
    Install kicad-lcsc-suite into KiCad's plugin directory (Windows).

.DESCRIPTION
    Creates a directory junction from KiCad's scripting/plugins folder to this
    repository checkout, so `git pull` updates the installed plugin with no
    reinstall step. Junctions do not need administrator rights (unlike
    symlinks), which is why they are used here.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Version 10.0
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
# Must be a valid Python identifier — the repo directory name has hyphens.
$LinkName = 'kicad_lcsc_suite'

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

$PluginDir = Join-Path $KicadDocs "$Version\scripting\plugins"
$Target = Join-Path $PluginDir $LinkName

if ($Uninstall) {
    if (Test-Path $Target) {
        $item = Get-Item $Target -Force
        if ($item.LinkType) {
            $item.Delete()
            Write-Host "Removed $Target"
        } else {
            throw "$Target exists but is not a junction; remove it by hand."
        }
    } else {
        Write-Host "Nothing installed at $Target"
    }
    return
}

New-Item -ItemType Directory -Force -Path $PluginDir | Out-Null

if (Test-Path $Target) {
    $item = Get-Item $Target -Force
    if ($item.LinkType) {
        $item.Delete()
    } else {
        throw "$Target exists and is not a junction. Move it aside, then re-run."
    }
}

New-Item -ItemType Junction -Path $Target -Value $Src | Out-Null

Write-Host "Installed kicad-lcsc-suite"
Write-Host "  source : $Src"
Write-Host "  target : $Target"
Write-Host "  KiCad  : $Version"
Write-Host ""
Write-Host "Restart KiCad, then open the PCB editor and use"
Write-Host "  Tools -> External Plugins -> LCSC Suite"
