<#
.SYNOPSIS
    Guard script for the retired TerminalStress agent daemon.
.DESCRIPTION
    The legacy daemon in src\monkey\agent_daemon.py has been replaced by the
    standalone agentinbox project. This script intentionally does NOT start the
    legacy daemon so Copilot sessions do not accidentally spawn the wrong
    background worker while operating inside the TerminalStress repo.

    Set TERMINALSTRESS_ENABLE_LEGACY_DAEMON=1 only if you explicitly need to
    run the retired daemon for a one-off migration/debug session.
#>

$running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*src\monkey\agent_daemon.py*' }

if ($env:TERMINALSTRESS_ENABLE_LEGACY_DAEMON -eq "1") {
    Write-Host "warning: legacy TerminalStress daemon override enabled." -ForegroundColor Yellow
    Write-Host "Run the retired daemon manually if you really need it:" -ForegroundColor Yellow
    Write-Host "  uv run python src\monkey\agent_daemon.py" -ForegroundColor Yellow
    return
}

Write-Host "Legacy TerminalStress daemon is disabled." -ForegroundColor Yellow
Write-Host "Use the standalone agentinbox daemon instead." -ForegroundColor Yellow
Write-Host "Example:" -ForegroundColor Yellow
Write-Host "  C:\Users\randy\Git\agentinbox\.venv\Scripts\python.exe -m agentinbox daemon --agent-name stressbot --working-directory `"$((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))`"" -ForegroundColor Yellow

if ($running) {
    Write-Host "Running legacy daemon PID(s): $($running.ProcessId -join ', ')" -ForegroundColor Yellow
}
