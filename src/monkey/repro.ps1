<#
.SYNOPSIS
    Reproduces a Windows Terminal crash (TerminalApp.dll ACCESS_VIOLATION)
    by running parallel monkey stress test instances.

.DESCRIPTION
    This script installs Python dependencies, then launches monkey tester
    instances that bombard Windows Terminal with randomized UI actions.
    After the test completes, it runs analyze-crashes.ps1 to correlate
    Windows Event Log crashes with test data.

    Prerequisites:
      - Python 3.10+ on PATH
      - Windows Terminal installed
      - This repo cloned

    The crash manifests as:
      Exception code: 0xc0000005 (ACCESS_VIOLATION) in TerminalApp.dll
      Followed by: 0xc000041d (FATAL_USER_CALLBACK_EXCEPTION)

.EXAMPLE
    .\repro.ps1
    .\repro.ps1 -Duration 60 -Instances 50
    .\repro.ps1 -Duration 30 -Instances 50 -NoAnalyze
#>
param(
    [int]$Duration = 30,
    [int]$Instances = 10,
    [int]$Seed = 666,
    [string]$WtProfile = "Command Prompt",
    [switch]$NoAnalyze
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $repoRoot) { $repoRoot = $PSScriptRoot }
$monkeyDir = Join-Path $repoRoot 'src' 'monkey'
$srcDir = Join-Path $repoRoot 'src'

Write-Host "=== Windows Terminal Crash Repro ===" -ForegroundColor Cyan
Write-Host "Duration: ${Duration}s, Instances: $Instances, Seed: $Seed"
Write-Host ""

# Record start time for event log correlation
$testStartTime = Get-Date

# Check for uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'uv' is not installed." -ForegroundColor Red
    Write-Host "Install it by running:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" -ForegroundColor White
    throw "'uv' is not installed. Please install it and try again."
}

# Install dependencies (prefer venv if one exists, fall back to --system)
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
$venvDir = Join-Path $repoRoot '.venv'
if (Test-Path (Join-Path $venvDir 'Scripts' 'Activate.ps1')) {
    & (Join-Path $venvDir 'Scripts' 'Activate.ps1')
    uv pip install -r (Join-Path $monkeyDir 'requirements.txt') --quiet
} else {
    uv pip install -r (Join-Path $monkeyDir 'requirements.txt') --quiet
}
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

# Clear old logs
$logDir = Join-Path $srcDir 'monkey_logs'
if (Test-Path $logDir) { Remove-Item -Recurse -Force $logDir }

# Launch monkey in conhost so it survives WT crashes, and wait for it to finish
Write-Host "Launching $Instances monkey instances in conhost..." -ForegroundColor Yellow
Write-Host "(This will take ~${Duration}s plus overhead)" -ForegroundColor DarkGray
$cmdArgs = "--duration $Duration --seed $Seed --launch --instances $Instances"
if ($WtProfile) {
    $cmdArgs += " --wt-profile `"$WtProfile`""
}

# Use venv python if available, otherwise system python
$pythonExe = "python"
if (Test-Path (Join-Path $repoRoot '.venv' 'Scripts' 'python.exe')) {
    $pythonExe = Join-Path $repoRoot '.venv' 'Scripts' 'python.exe'
}

# Write a temp batch file to avoid nested quoting issues with conhost/cmd
$batchFile = Join-Path $srcDir '_run_monkey.cmd'
$batchContent = "@echo off`r`ncd /d `"$srcDir`"`r`n`"$pythonExe`" -m monkey.runner $cmdArgs`r`n"
Set-Content -Path $batchFile -Value $batchContent -Encoding ASCII

$conhostProc = Start-Process conhost.exe `
    -ArgumentList "`"$batchFile`"" `
    -PassThru

Write-Host ""
Write-Host "Monkey tester running in conhost (PID $($conhostProc.Id))..." -ForegroundColor Green
Write-Host "Watch Windows Terminal for crashes. Logs: $logDir" -ForegroundColor Green
Write-Host ""

# Wait for the conhost process to exit
Write-Host "Waiting for test to complete..." -ForegroundColor Yellow
$conhostProc.WaitForExit()
$testEndTime = Get-Date

Write-Host ""
Write-Host "Test completed in $([math]::Round(($testEndTime - $testStartTime).TotalSeconds))s" -ForegroundColor Cyan
Write-Host ""

# Give event log a moment to flush
Start-Sleep -Seconds 3

# Clean up temp batch file
if (Test-Path $batchFile) { Remove-Item $batchFile -ErrorAction SilentlyContinue }

# Run crash analysis
if (-not $NoAnalyze) {
    $analyzeScript = Join-Path $monkeyDir 'analyze-crashes.ps1'
    Write-Host "Running crash analysis..." -ForegroundColor Yellow
    Write-Host ""
    & $analyzeScript -Since $testStartTime -Until $testEndTime.AddSeconds(10) -Detailed
}
else {
    Write-Host "Skipping analysis (-NoAnalyze). Run manually:" -ForegroundColor Yellow
    Write-Host "  .\analyze-crashes.ps1 -Since '$($testStartTime.ToString('yyyy-MM-dd HH:mm:ss'))'" -ForegroundColor White
}
