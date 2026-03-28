# TerminalStress
A small application to stress test Windows Terminal

## Build & Run

```bash
# Build
dotnet build src/TerminalStress.sln

# Run (UTF-8 mode, default)
dotnet run --project src/TerminalStress.csproj

# Run (UTF-7 mode, triggered by passing any argument)
dotnet run --project src/TerminalStress.csproj -- anyarg
```

## Legacy Agent Daemon

The old in-repo daemon (`src/monkey/agent_daemon.py`) is intentionally
disabled so Copilot sessions do not accidentally launch it while working in
this repo. The standalone `agentinbox` project now owns background GroupMe
queue processing.

The guard script below is safe to run, but it will only print guidance:

```bash
.\src\monkey\ensure-daemon.ps1
```

To run the active daemon for TerminalStress directives, use the standalone
project instead:

```powershell
C:\Users\randy\Git\agentinbox\.venv\Scripts\python.exe -m agentinbox daemon --agent-name stressbot --working-directory "C:\Users\randy\Git\TerminalStress"
```

## Legacy Windows Service

Any old TerminalStress-specific service that launches `src\monkey\agent_daemon.py`
should be considered retired and replaced by the standalone `agentinbox`
service/daemon flow.

### Install

```powershell
# Publish (from repo root)
cd src\service
dotnet publish -c Release -o ..\..\publish\service

# Install the service (requires admin/elevated shell)
StressBotService install

# Start it now
StressBotService start
```

### Manage

```powershell
StressBotService status              # Show service status and config summary
StressBotService stop                # Stop the service
StressBotService start               # Start the service
StressBotService uninstall           # Remove the service
```

### Configure

Configuration lives in `stressbot-service.json` next to the published exe.

```powershell
StressBotService config show                          # View current config
StressBotService config set arguments "--interval 15" # Pass args to daemon
StressBotService config set restartDelaySeconds 10    # Change restart delay
StressBotService config set enabled false             # Disable without uninstalling
StressBotService config reset                         # Reset to defaults
```

Key config options:

| Key | Default | Description |
|-----|---------|-------------|
| `pythonPath` | `.venv\Scripts\python.exe` | Python interpreter (relative to workingDirectory) |
| `scriptPath` | `src\monkey\agent_daemon.py` | Daemon script path |
| `workingDirectory` | Repo root | Working directory for the daemon |
| `arguments` | `""` | Extra args passed to agent_daemon.py |
| `restartOnCrash` | `true` | Auto-restart on unexpected exit |
| `restartDelaySeconds` | `5` | Delay before restart |
| `maxRestarts` | `10` | Max restarts within the window |
| `maxRestartWindowMinutes` | `60` | Restart counting window |
| `extraPath` | `""` | Extra PATH entries (needed for copilot CLI) |
| `enabled` | `true` | Master on/off switch |

### Logs

The service redirects daemon stdout/stderr to `src/monkey_logs/`:
- `service_stdout.log` — daemon stdout
- `service_stderr.log` — daemon stderr
- `daemon.jsonl` — structured daemon event log (written by the daemon itself)

## Web Dashboard

A real-time monitoring dashboard with live log streaming, charts, and status panels.

```bash
# Install dependencies (once)
uv pip install fastapi uvicorn[standard] aiofiles

# Launch the dashboard
python src/dashboard/server.py
```

Open [http://localhost:8420](http://localhost:8420) in your browser.

**Features:**
- Live daemon log stream (Server-Sent Events) with color-coded event tags
- Inbox directives table — sender, instruction, status, response time
- Chart.js visualizations — action distribution, memory usage, crash timeline, directive response times
- Crash dump inventory and campaign history
- Stress test results table with leak detection
- Auto-refreshes every 10 seconds
