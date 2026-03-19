<#
.SYNOPSIS
    Reproduces a Windows Terminal crash (TerminalApp.dll ACCESS_VIOLATION)
    by running 10 parallel monkey stress test instances.

.DESCRIPTION
    This script installs Python dependencies, then launches 10 monkey tester
    instances that bombard Windows Terminal with randomized UI actions.
    The crash typically occurs within 30 seconds.

    Prerequisites:
      - Python 3.10+ on PATH
      - Windows Terminal installed
      - This repo cloned

    The crash manifests as:
      Exception code: 0xc0000005 (ACCESS_VIOLATION) in TerminalApp.dll
      Followed by: 0xc000041d (FATAL_USER_CALLBACK_EXCEPTION)

.EXAMPLE
    .\repro.ps1
    .\repro.ps1 -Duration 60 -Instances 5
#>
param(
    [int]$Duration = 30,
    [int]$Instances = 10,
    [int]$Seed = 666
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $repoRoot) { $repoRoot = $PSScriptRoot }
$monkeyDir = Join-Path $repoRoot 'src' 'monkey'
$srcDir = Join-Path $repoRoot 'src'

Write-Host "=== Windows Terminal Crash Repro ===" -ForegroundColor Cyan
Write-Host "Duration: ${Duration}s, Instances: $Instances, Seed: $Seed"
Write-Host ""

# Check for uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'uv' is not installed." -ForegroundColor Red
    Write-Host "Install it by running:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" -ForegroundColor White
    throw "'uv' is not installed. Please install it and try again."
}

# Install dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
uv pip install -r (Join-Path $monkeyDir 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

# Clear old logs
$logDir = Join-Path $srcDir 'monkey_logs'
if (Test-Path $logDir) { Remove-Item -Recurse -Force $logDir }

# Launch monkey in conhost so it survives WT crashes
Write-Host "Launching $Instances monkey instances in conhost..." -ForegroundColor Yellow
$cmdArgs = "--duration $Duration --seed $Seed --launch --instances $Instances"
Start-Process conhost.exe -ArgumentList "cmd /k `"cd /d $srcDir && python -m monkey.runner $cmdArgs`""

Write-Host ""
Write-Host "Monkey tester is running in a separate conhost window." -ForegroundColor Green
Write-Host "Watch Windows Terminal for the crash. Logs will be in:" -ForegroundColor Green
Write-Host "  $logDir" -ForegroundColor White
Write-Host ""
Write-Host "After the test completes, check for crashes:" -ForegroundColor Yellow
Write-Host "  Get-ChildItem $logDir\*.log | ForEach-Object { Select-String 'CRASHED' `$_ }" -ForegroundColor White
Write-Host ""
Write-Host "Check Windows Event Log:" -ForegroundColor Yellow
Write-Host "  Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'; StartTime=(Get-Date).AddMinutes(-5)} | Where-Object { `$_.Message -match 'Terminal' } | Format-List TimeCreated, Message" -ForegroundColor White
