# TerminalStress

`TerminalStress` is a monkey-testing and crash-analysis repo for Windows
Terminal. It focuses on:

- running aggressive monkey scenarios against Windows Terminal
- collecting crash dumps and health summaries
- analyzing crash patterns
- publishing crash reports to `rtreit.com`

Remote control and GroupMe-driven agent execution now live in the standalone
`agentinbox` project, not in this repo.

## Build and Run

```powershell
# Build the console app
dotnet build src\TerminalStress.sln

# Run the console stress app directly
dotnet run --project src\TerminalStress.csproj

# Run with UTF-7 output mode
dotnet run --project src\TerminalStress.csproj -- anyarg
```

## Monkey Runner

For real stress runs, prefer the helper that launches in a visible
`conhost.exe` window:

```powershell
src\monkey\run_monkey.cmd --duration 600 --launch --action-profile buffer-chaos
```

You can also invoke the Python runner directly:

```powershell
Set-Location src
uv run python -m monkey.runner --duration 300 --launch
uv run python -m monkey.runner --duration 3600 --action-profile scroll-race
uv run python -m monkey.runner --duration 0 --instances 4 --action-profile novelty-hunt
```

Monkey run logs and summaries are written under `src\monkey_logs\`.

## Crash Analysis

```powershell
# Analyze recent crash dumps and summaries
src\monkey\analyze-crashes.ps1

# Generate an HTML crash report from collected dumps and summaries
uv run python src\monkey\generate_crash_report.py
```

Crash dumps live in `crashdumps\`. Structured analysis output is written under
`crashdumps\.analysis\`.

## Report Upload

TerminalStress still supports publishing crash analysis reports to
`rtreit.com`:

```powershell
uv run python src\monkey\upload_report.py crashdumps\crash-analysis-report.html
uv run python src\monkey\upload_report.py crashdumps\crash-analysis-report.html --name "overnight-crashes.html"
```

Set `RTREIT_REPORTS_API_KEY` in a local `.env` file or environment variable.

## Web Dashboard

The dashboard is focused on monkey telemetry, crash inventory, campaigns, and
summary history.

```powershell
# Install dashboard dependencies if needed
uv pip install fastapi uvicorn[standard]

# Launch the dashboard
uv run python src\dashboard\server.py
```

Open `http://localhost:8420` in your browser.

## Remote Control

If you want to drive TerminalStress through chat/queues/agents, use the
standalone `agentinbox` project externally and point it at this repo's working
directory. TerminalStress itself no longer owns inbox, daemon, or Windows
service functionality.
