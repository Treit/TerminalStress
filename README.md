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

## Agent Daemon

The monkey stress tester includes an agent daemon (`src/monkey/agent_daemon.py`) that polls a GroupMe inbox queue and dispatches directives to the Copilot CLI. To start it manually:

```bash
.\src\monkey\ensure-daemon.ps1
```

## Windows Service (Auto-Start on Boot)

A .NET 8 Windows service ensures the agent daemon survives reboots. The service launches `agent_daemon.py` using the repo's `.venv`, monitors the process, and restarts it on crash.

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
