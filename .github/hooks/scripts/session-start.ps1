<#
.SYNOPSIS
    Session start hook for TerminalStress.
.DESCRIPTION
    Prints a brief reminder that this repo is monkey/crashdump-focused and
    shows recent crash dumps and summaries for quick context.
#>

$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) { $repoRoot = $PSScriptRoot -replace '[\\/]\.github[\\/]hooks[\\/]scripts$', '' }

Write-Host "TerminalStress session ready - no inbox daemon is managed in this repo." -ForegroundColor DarkGray

$crashDir = Join-Path $repoRoot "crashdumps"
$logDir = Join-Path $repoRoot "src\monkey_logs"

$recentDumps = Get-ChildItem $crashDir -Filter *.dmp -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 3
if ($recentDumps) {
    Write-Host "Recent crash dumps:" -ForegroundColor DarkGray
    $recentDumps | ForEach-Object { Write-Host "  $($_.Name)" -ForegroundColor DarkGray }
}

$recentSummaries = Get-ChildItem $logDir -Filter summary_*.json -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 3
if ($recentSummaries) {
    Write-Host "Recent summary files:" -ForegroundColor DarkGray
    $recentSummaries | ForEach-Object { Write-Host "  $($_.Name)" -ForegroundColor DarkGray }
}
if (-not $recentDumps -and -not $recentSummaries) {
    Write-Host "No recent crash dumps or summaries found." -ForegroundColor DarkGray
}
    }
}
