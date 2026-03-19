<#
.SYNOPSIS
    Configures Windows Error Reporting to save full crash dumps for
    WindowsTerminal.exe. Requires admin elevation.

.DESCRIPTION
    Sets the WER LocalDumps registry key so that any WindowsTerminal.exe
    crash automatically saves a full memory dump to the crashdumps folder.
    This is essential for post-crash analysis with WinDbg/cdb.

    Run with -Remove to undo the configuration.

.EXAMPLE
    # Enable (requires Run as Administrator)
    .\setup-crashdumps.ps1

    # Disable
    .\setup-crashdumps.ps1 -Remove

    # Custom dump folder and max count
    .\setup-crashdumps.ps1 -DumpFolder C:\mycrashes -DumpCount 50
#>
param(
    [string]$DumpFolder,
    [int]$DumpCount = 20,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$regPath = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\WindowsTerminal.exe"

if ($Remove) {
    if (Test-Path $regPath) {
        Remove-Item $regPath -Recurse -Force
        Write-Host "Removed WER LocalDumps config for WindowsTerminal.exe" -ForegroundColor Green
    }
    else {
        Write-Host "No config to remove (key does not exist)" -ForegroundColor Yellow
    }
    return
}

# Default dump folder is crashdumps\ in the repo root
if (-not $DumpFolder) {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (-not $repoRoot) { $repoRoot = Split-Path -Parent $PSScriptRoot }
    $DumpFolder = Join-Path $repoRoot 'crashdumps'
}

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Host "ERROR: This script requires Administrator privileges." -ForegroundColor Red
    Write-Host "Re-run from an elevated PowerShell, or use:" -ForegroundColor Yellow
    Write-Host "  Start-Process pwsh -Verb RunAs -ArgumentList '-File', '$($MyInvocation.MyCommand.Path)'" -ForegroundColor White
    exit 1
}

# Create dump folder
New-Item -ItemType Directory -Path $DumpFolder -Force | Out-Null

# Set registry keys
New-Item -Path $regPath -Force | Out-Null
Set-ItemProperty -Path $regPath -Name "DumpFolder" -Value $DumpFolder -Type ExpandString
Set-ItemProperty -Path $regPath -Name "DumpCount" -Value $DumpCount -Type DWord
Set-ItemProperty -Path $regPath -Name "DumpType" -Value 2 -Type DWord  # 2 = Full dump

Write-Host "WER LocalDumps configured for WindowsTerminal.exe:" -ForegroundColor Green
Write-Host "  DumpFolder : $DumpFolder" -ForegroundColor White
Write-Host "  DumpCount  : $DumpCount" -ForegroundColor White
Write-Host "  DumpType   : 2 (Full dump)" -ForegroundColor White
Write-Host ""
Write-Host "Crash dumps will now be saved automatically when WT crashes." -ForegroundColor Cyan
Write-Host "Analyze with: cdb -z <dump.dmp> -c `".ecxr; !analyze -v; k; q`"" -ForegroundColor Cyan
Write-Host ""
Write-Host "To remove: .\setup-crashdumps.ps1 -Remove" -ForegroundColor DarkGray
