# Copilot Instructions

## Session Startup

This repo no longer owns an inbox daemon, GroupMe bot workflow, or Windows
service. Do **not** run legacy daemon or inbox commands here.

Start with the user's request. When useful, inspect recent crash dumps,
monkey summaries, and monkey logs:

```powershell
Get-ChildItem crashdumps -Filter *.dmp -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5
Get-ChildItem src\monkey_logs -Filter summary_*.json -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

If remote/background control is needed, use the standalone `agentinbox`
project externally and point it at this repo's working directory.

## Build and Run

```powershell
# Build the console app
dotnet build src\TerminalStress.sln

# Run the console app directly
dotnet run --project src\TerminalStress.csproj

# Run with UTF-7 output mode
dotnet run --project src\TerminalStress.csproj -- anyarg
```

## Monkey Workflows

Always prefer the helper that launches the monkey in a visible `conhost.exe`
window:

```powershell
src\monkey\run_monkey.cmd --duration 600 --launch --action-profile buffer-chaos
```

You can also invoke the runner directly:

```powershell
Set-Location src
uv run python -m monkey.runner --duration 300 --launch
uv run python -m monkey.runner --duration 0 --instances 4 --action-profile novelty-hunt
```

## Architecture

- `src\Program.cs` is the simple .NET console stress app
- `src\monkey\runner.py` is the main Windows Terminal monkey harness
- `src\monkey_logs\` stores monkey logs, summaries, and campaign files
- `crashdumps\` stores collected dumps and generated analysis artifacts
- `src\dashboard\` serves a local crash/telemetry dashboard

## Conventions

- Target framework is .NET 8.0 (`net8.0`)
- Use `uv` instead of `pip` for Python workflows
- Use `es` (Everything Search CLI) to locate files quickly on disk
- Keep this repo focused on monkey runs, crash discovery, crash analysis, and report publishing
- Do not recreate inbox/daemon/service functionality in this repo; that belongs in `agentinbox`

## Crash Analysis and Reporting

```powershell
# Analyze current dumps and summaries
src\monkey\analyze-crashes.ps1

# Generate an HTML report
uv run python src\monkey\generate_crash_report.py

# Upload the report to rtreit.com
uv run python src\monkey\upload_report.py crashdumps\crash-analysis-report.html
```

Set `RTREIT_REPORTS_API_KEY` in a local `.env` file or environment variable
when upload is needed.
