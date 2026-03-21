<#
.SYNOPSIS
    Ensures the agent daemon is running. Starts it if not.
.DESCRIPTION
    Checks if agent_daemon.py is already running. If not, launches it
    as a detached background process in a visible conhost window.
    Safe to call multiple times — it's a no-op if already running.
#>

$daemonScript = Join-Path $PSScriptRoot "agent_daemon.py"
$python = "C:\Users\randy\AppData\Local\Programs\Python\Python313\python.exe"

if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}

if (-not $python) {
    Write-Host "warning: python not found, cannot start daemon" -ForegroundColor Yellow
    return
}

# Check if already running
$running = Get-Process python* -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'agent_daemon' }

if ($running) {
    Write-Host "Agent daemon already running (PID $($running.Id -join ', '))" -ForegroundColor Green
    return
}

# Start in a new conhost window so it survives and is visible
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Write-Host "Starting agent daemon..." -ForegroundColor Cyan
Start-Process conhost.exe -ArgumentList "cmd /k `"cd /d $repoRoot && `"$python`" src\monkey\agent_daemon.py`""
Write-Host "Agent daemon started in new window" -ForegroundColor Green
