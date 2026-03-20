<#
.SYNOPSIS
    Analyzes monkey stress test results by correlating Windows Event Log
    crashes with monkey test logs and summaries.

.DESCRIPTION
    Run this after a monkey stress test to find ALL crashes, including those
    missed by the watchdog's process-polling approach.

    Checks:
      1. Windows Event Log for Application Error events (TerminalApp.dll,
         WindowsTerminal.exe crashes)
      2. Monkey summary JSONs for PIDs, seeds, action counts
      3. Monkey log files for hangs, errors, and crash indicators

    Correlates event log crash PIDs with monkey-tracked PIDs and produces
    a consolidated report.

.PARAMETER LogDir
    Path to the monkey_logs directory. Defaults to src\monkey_logs relative
    to the repo root.

.PARAMETER Since
    Only look at events after this time. Accepts a [datetime] or a string
    parseable by Get-Date. Defaults to 1 hour ago.

.PARAMETER Until
    Only look at events before this time. Defaults to now.

.PARAMETER Detailed
    Show full event log messages and log excerpts.

.EXAMPLE
    .\analyze-crashes.ps1
    .\analyze-crashes.ps1 -Since "2026-03-19 14:50:00" -Detailed
    .\analyze-crashes.ps1 -LogDir C:\custom\logs -Since (Get-Date).AddMinutes(-10)
#>
param(
    [string]$LogDir,
    [datetime]$Since = (Get-Date).AddHours(-1),
    [datetime]$Until = (Get-Date),
    [switch]$Detailed
)

$ErrorActionPreference = 'Continue'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $repoRoot) { $repoRoot = Split-Path -Parent $PSScriptRoot }
if (-not $LogDir) { $LogDir = Join-Path $repoRoot 'src' 'monkey_logs' }

# ── Banner ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║       Windows Terminal Crash Analysis Report            ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host "  Time window : $($Since.ToString('yyyy-MM-dd HH:mm:ss')) → $($Until.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "  Log dir     : $LogDir"
Write-Host ""

# ── 1. Query Windows Event Log ─────────────────────────────────────────
Write-Host "━━━ 1. Windows Event Log Crashes ━━━" -ForegroundColor Yellow

$crashEvents = @()
try {
    # Application Error events
    $appErrors = Get-WinEvent -FilterHashtable @{
        LogName      = 'Application'
        ProviderName = 'Application Error'
        StartTime    = $Since
        EndTime      = $Until
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -match 'WindowsTerminal|TerminalApp'
    }
    if ($appErrors) { $crashEvents += $appErrors }

    # .NET Runtime / WER events that mention Terminal
    $werEvents = Get-WinEvent -FilterHashtable @{
        LogName   = 'Application'
        StartTime = $Since
        EndTime   = $Until
        Id        = 1000, 1001, 1002  # WER report events
    } -ErrorAction SilentlyContinue | Where-Object {
        $_.Message -match 'WindowsTerminal|TerminalApp'
    }
    if ($werEvents) { $crashEvents += $werEvents }
}
catch {
    # No events found is fine
}

# Deduplicate by TimeCreated (same crash can generate multiple event types)
$crashEvents = $crashEvents |
    Sort-Object TimeCreated -Unique

if ($crashEvents.Count -eq 0) {
    Write-Host "  No Windows Terminal crash events found in Event Log." -ForegroundColor Green
}
else {
    Write-Host "  Found $($crashEvents.Count) crash event(s):" -ForegroundColor Red

    $eventSummaries = @()
    foreach ($evt in $crashEvents) {
        $msg = $evt.Message
        # Parse the faulting PID (hex) from the event message
        $crashPid = $null
        if ($msg -match 'Faulting process id:\s*0x([0-9A-Fa-f]+)') {
            $crashPid = [Convert]::ToInt32($Matches[1], 16)
        }

        # Parse exception code
        $exceptionCode = $null
        if ($msg -match 'Exception code:\s*(0x[0-9A-Fa-f]+)') {
            $exceptionCode = $Matches[1]
        }

        # Parse faulting module
        $faultModule = $null
        if ($msg -match 'Faulting module name:\s*(\S+)') {
            $faultModule = $Matches[1].TrimEnd(',')
        }

        # Parse fault offset
        $faultOffset = $null
        if ($msg -match 'Fault offset:\s*(0x[0-9A-Fa-f]+)') {
            $faultOffset = $Matches[1]
        }

        $summary = [PSCustomObject]@{
            Time          = $evt.TimeCreated
            PID           = $crashPid
            ExceptionCode = $exceptionCode
            Module        = $faultModule
            Offset        = $faultOffset
            EventId       = $evt.Id
        }
        $eventSummaries += $summary

        $pidStr = if ($crashPid) { "PID $crashPid (0x$($crashPid.ToString('X')))" } else { "PID unknown" }
        $codeDesc = switch ($exceptionCode) {
            '0xc0000005' { 'ACCESS_VIOLATION' }
            '0xc000041d' { 'FATAL_USER_CALLBACK_EXCEPTION' }
            '0xc0000374' { 'HEAP_CORRUPTION' }
            '0xc00000fd' { 'STACK_OVERFLOW' }
            '0xc0000409' { 'STACK_BUFFER_OVERRUN' }
            '0x80000003' { 'BREAKPOINT' }
            default { '' }
        }
        $codeLabel = if ($codeDesc) { "$exceptionCode ($codeDesc)" } else { $exceptionCode }

        Write-Host ""
        Write-Host "  ┌ Crash @ $($evt.TimeCreated.ToString('HH:mm:ss.fff'))" -ForegroundColor Red
        Write-Host "  │ $pidStr" -ForegroundColor White
        Write-Host "  │ Exception: $codeLabel" -ForegroundColor White
        Write-Host "  │ Module: $faultModule  Offset: $faultOffset" -ForegroundColor White

        if ($Detailed) {
            Write-Host "  │" -ForegroundColor DarkGray
            $msg -split "`n" | ForEach-Object {
                Write-Host "  │ $_" -ForegroundColor DarkGray
            }
        }
        Write-Host "  └" -ForegroundColor Red
    }
}

# ── 2. Parse Monkey Summary JSONs ──────────────────────────────────────
Write-Host ""
Write-Host "━━━ 2. Monkey Test Summaries ━━━" -ForegroundColor Yellow

$summaryFiles = @()
if (Test-Path $LogDir) {
    $summaryFiles = Get-ChildItem -Path $LogDir -Filter 'summary_*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $Since.AddMinutes(-1) -and $_.LastWriteTime -le $Until.AddMinutes(1) } |
        Sort-Object LastWriteTime
}

$monkeyPidMap = @{}    # pid -> list of summary objects
$monkeySeeds = @{}     # seed -> summary

if ($summaryFiles.Count -eq 0) {
    Write-Host "  No summary JSON files found in $LogDir" -ForegroundColor DarkGray
}
else {
    Write-Host "  Found $($summaryFiles.Count) summary file(s):" -ForegroundColor White
    $totalActions = 0
    $totalCrashes = 0
    $totalHangs = 0

    foreach ($sf in $summaryFiles) {
        try {
            $s = Get-Content $sf.FullName -Raw | ConvertFrom-Json
            $sPid = [int]$s.pid
            $sSeed = $s.seed
            $profile = if ($s.PSObject.Properties.Name -contains 'action_profile' -and $s.action_profile) {
                $s.action_profile
            } else {
                'default'
            }

            if (-not $monkeyPidMap.ContainsKey($sPid)) {
                $monkeyPidMap[$sPid] = @()
            }
            $monkeyPidMap[$sPid] += $s
            if ($sSeed) { $monkeySeeds[$sSeed] = $s }

            $totalActions += [int]($s.total_actions)
            if ($s.crash_detected) { $totalCrashes++ }
            if ($s.hang_count -gt 0) { $totalHangs++ }

            $status = if ($s.crash_detected) { "CRASH" }
                      elseif ($s.hang_count -gt 0) { "HANG($($s.hang_count))" }
                      else { "OK" }
            $statusColor = switch -Wildcard ($status) {
                'CRASH' { 'Red' }
                'HANG*' { 'Yellow' }
                default { 'Green' }
            }
            Write-Host "    PID=$sPid  seed=$sSeed  profile=$profile  actions=$($s.total_actions)  " -NoNewline
            Write-Host "$status" -ForegroundColor $statusColor -NoNewline
            Write-Host "  mem=$($s.initial_rss_mb)→$($s.peak_rss_mb)MB  dur=$($s.duration_seconds)s"

            $tagProps = @()
            if ($s.PSObject.Properties.Name -contains 'tag_counts' -and $s.tag_counts) {
                $tagProps = @($s.tag_counts.PSObject.Properties | Sort-Object { [int]$_.Value } -Descending | Select-Object -First 3)
            }
            if ($tagProps.Count -gt 0) {
                $topTags = ($tagProps | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', '
                Write-Host "      Top tags: $topTags" -ForegroundColor DarkGray
            }

            $lastCrash = @($s.crash_events) | Select-Object -Last 1
            if ($lastCrash -and $lastCrash.recent_actions) {
                Write-Host "      Last crash context: $(($lastCrash.recent_actions) -join ' -> ')" -ForegroundColor DarkYellow
            }

            $lastHang = @($s.hang_events) | Select-Object -Last 1
            if ($lastHang -and $lastHang.recent_actions) {
                Write-Host "      Last hang context:  $(($lastHang.recent_actions) -join ' -> ')" -ForegroundColor DarkYellow
            }
        }
        catch {
            Write-Host "    [!] Failed to parse $($sf.Name): $_" -ForegroundColor Red
        }
    }

    Write-Host ""
    Write-Host "  Totals: $totalActions actions, $totalCrashes watchdog-detected crashes, $totalHangs hang sessions" -ForegroundColor White

    $profileSummaries = foreach ($sf in $summaryFiles) {
        try {
            $s = Get-Content $sf.FullName -Raw | ConvertFrom-Json
            [PSCustomObject]@{
                Profile = if ($s.PSObject.Properties.Name -contains 'action_profile' -and $s.action_profile) { $s.action_profile } else { 'default' }
                Actions = [int]$s.total_actions
                Crashes = if ($s.PSObject.Properties.Name -contains 'total_crashes') { [int]$s.total_crashes } else { 0 }
                Hangs = [int]$s.hang_count
            }
        } catch {
        }
    }

    if ($profileSummaries) {
        Write-Host "  By profile:" -ForegroundColor White
        $profileSummaries |
            Group-Object Profile |
            Sort-Object Name |
            ForEach-Object {
                $actions = ($_.Group | Measure-Object -Property Actions -Sum).Sum
                $crashes = ($_.Group | Measure-Object -Property Crashes -Sum).Sum
                $hangs = ($_.Group | Measure-Object -Property Hangs -Sum).Sum
                Write-Host "    $($_.Name): runs=$($_.Count), actions=$actions, crashes=$crashes, hangs=$hangs" -ForegroundColor DarkGray
            }
    }
}

# ── 3. Correlate Event Log PIDs with Monkey PIDs ──────────────────────
Write-Host ""
Write-Host "━━━ 3. PID Correlation ━━━" -ForegroundColor Yellow

if ($crashEvents.Count -gt 0 -and $summaryFiles.Count -gt 0) {
    $correlated = 0
    $uncorrelated = 0

    foreach ($es in $eventSummaries) {
        $crashPid = $es.PID
        if (-not $crashPid) {
            Write-Host "  [?] Event at $($es.Time.ToString('HH:mm:ss')) — could not parse PID" -ForegroundColor DarkGray
            $uncorrelated++
            continue
        }

        if ($monkeyPidMap.ContainsKey($crashPid)) {
            $matched = $monkeyPidMap[$crashPid]
            foreach ($m in $matched) {
                $correlated++
                Write-Host "  [✓] Event PID $crashPid ($($es.ExceptionCode)) ↔ Monkey seed=$($m.seed), actions=$($m.total_actions)" -ForegroundColor Green
                if (-not $m.crash_detected) {
                    Write-Host "      ⚠ Watchdog did NOT detect this crash (PID was being monitored)" -ForegroundColor Yellow
                }
            }
        }
        else {
            $uncorrelated++
            Write-Host "  [✗] Event PID $crashPid ($($es.ExceptionCode)) — NOT tracked by any monkey instance" -ForegroundColor DarkYellow
            Write-Host "      (WT may have restarted under a new PID before monkey connected)" -ForegroundColor DarkGray
        }
    }

    Write-Host ""
    Write-Host "  Correlated: $correlated  Uncorrelated: $uncorrelated" -ForegroundColor White
}
elseif ($crashEvents.Count -gt 0) {
    Write-Host "  No monkey summaries to correlate with. Event log PIDs:" -ForegroundColor DarkGray
    foreach ($es in $eventSummaries) {
        Write-Host "    PID=$($es.PID)  $($es.ExceptionCode)  @ $($es.Time.ToString('HH:mm:ss'))" -ForegroundColor White
    }
}
else {
    Write-Host "  No crash events to correlate." -ForegroundColor Green
}

# ── 4. Parse Monkey Logs for Hangs & Errors ────────────────────────────
Write-Host ""
Write-Host "━━━ 4. Monkey Log Analysis ━━━" -ForegroundColor Yellow

$logFiles = @()
if (Test-Path $LogDir) {
    $logFiles = Get-ChildItem -Path $LogDir -Filter 'monkey_*.log' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $Since.AddMinutes(-1) -and $_.LastWriteTime -le $Until.AddMinutes(1) } |
        Sort-Object Name
}

if ($logFiles.Count -eq 0) {
    Write-Host "  No log files found." -ForegroundColor DarkGray
}
else {
    Write-Host "  Scanning $($logFiles.Count) log file(s)..." -ForegroundColor White

    $hangLogs = @()
    $errorLogs = @()
    $crashLogs = @()

    foreach ($lf in $logFiles) {
        $content = Get-Content $lf.FullName -ErrorAction SilentlyContinue
        if (-not $content) { continue }

        $hangs = $content | Select-String 'CONFIRMED HANG|NOT RESPONDING'
        $errors = $content | Select-String 'CRASHED|FAILED|Unexpected error'
        $crashes = $content | Select-String 'Crash detected: True|CRASHED'

        if ($hangs) {
            $hangLogs += [PSCustomObject]@{ File = $lf.Name; Count = $hangs.Count; Lines = $hangs }
        }
        if ($errors) {
            $errorLogs += [PSCustomObject]@{ File = $lf.Name; Count = $errors.Count; Lines = $errors }
        }
        if ($crashes) {
            $crashLogs += [PSCustomObject]@{ File = $lf.Name; Count = $crashes.Count; Lines = $crashes }
        }
    }

    if ($hangLogs.Count -gt 0) {
        Write-Host "  Hangs detected in $($hangLogs.Count) log(s):" -ForegroundColor Yellow
        foreach ($h in $hangLogs) {
            Write-Host "    $($h.File): $($h.Count) hang indicator(s)" -ForegroundColor Yellow
            if ($Detailed) {
                foreach ($line in $h.Lines) {
                    Write-Host "      $($line.Line.Trim())" -ForegroundColor DarkYellow
                }
            }
        }
    }

    if ($crashLogs.Count -gt 0) {
        Write-Host "  Crashes detected in $($crashLogs.Count) log(s):" -ForegroundColor Red
        foreach ($c in $crashLogs) {
            Write-Host "    $($c.File): $($c.Count) crash indicator(s)" -ForegroundColor Red
            if ($Detailed) {
                foreach ($line in $c.Lines) {
                    Write-Host "      $($line.Line.Trim())" -ForegroundColor DarkRed
                }
            }
        }
    }

    if ($errorLogs.Count -gt 0) {
        Write-Host "  Errors found in $($errorLogs.Count) log(s):" -ForegroundColor DarkYellow
        foreach ($e in $errorLogs) {
            Write-Host "    $($e.File): $($e.Count) error(s)" -ForegroundColor DarkYellow
        }
    }

    if ($hangLogs.Count -eq 0 -and $crashLogs.Count -eq 0 -and $errorLogs.Count -eq 0) {
        Write-Host "  No hangs, crashes, or errors found in logs." -ForegroundColor Green
    }
}

# ── 5. Unique Crash Signatures ─────────────────────────────────────────
Write-Host ""
Write-Host "━━━ 5. Unique Crash Signatures ━━━" -ForegroundColor Yellow

if ($eventSummaries.Count -gt 0) {
    $signatures = $eventSummaries | Group-Object { "$($_.Module)|$($_.ExceptionCode)|$($_.Offset)" }
    Write-Host "  $($signatures.Count) unique crash signature(s):" -ForegroundColor White

    foreach ($sig in $signatures) {
        $first = $sig.Group[0]
        $codeDesc = switch ($first.ExceptionCode) {
            '0xc0000005' { 'ACCESS_VIOLATION' }
            '0xc000041d' { 'FATAL_USER_CALLBACK_EXCEPTION' }
            '0xc0000374' { 'HEAP_CORRUPTION' }
            '0xc00000fd' { 'STACK_OVERFLOW' }
            '0xc0000409' { 'STACK_BUFFER_OVERRUN' }
            '0x80000003' { 'BREAKPOINT' }
            default { '' }
        }
        Write-Host ""
        Write-Host "  ┌ $($first.Module) + $($first.Offset)" -ForegroundColor Magenta
        Write-Host "  │ $($first.ExceptionCode) $(if($codeDesc){"($codeDesc)"})" -ForegroundColor White
        Write-Host "  │ Hit $($sig.Count) time(s)" -ForegroundColor White
        Write-Host "  │ Times: $(($sig.Group | ForEach-Object { $_.Time.ToString('HH:mm:ss') }) -join ', ')" -ForegroundColor DarkGray
        Write-Host "  └" -ForegroundColor Magenta
    }
}
else {
    Write-Host "  No crash signatures (no event log crashes found)." -ForegroundColor Green
}

# ── Final Verdict ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
$totalEventCrashes = $crashEvents.Count
$totalWatchdogCrashes = ($summaryFiles | ForEach-Object {
    try { (Get-Content $_.FullName -Raw | ConvertFrom-Json).crash_detected } catch { $false }
} | Where-Object { $_ -eq $true }).Count

if ($totalEventCrashes -gt 0) {
    Write-Host "  RESULT: $totalEventCrashes Event Log crash(es) detected" -ForegroundColor Red
    if ($totalWatchdogCrashes -lt $totalEventCrashes) {
        $missed = $totalEventCrashes - $totalWatchdogCrashes
        Write-Host "  ⚠ Watchdog missed $missed crash(es) — Event Log is the source of truth" -ForegroundColor Yellow
    }
}
else {
    Write-Host "  RESULT: No crashes detected" -ForegroundColor Green
}
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""
