# Session Startup

`TerminalStress` no longer owns an inbox daemon or queue workflow.

Do **not** run `ensure-daemon.ps1`, `agent_inbox.py`, or other retired
Agent Inbox helpers in this repo.

Start with the user's request. If you need recent context, inspect crash dumps,
monkey logs, and summary files instead:

```powershell
Get-ChildItem crashdumps -Filter *.dmp -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5
Get-ChildItem src\monkey_logs -Filter monkey_*.log -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5
Get-ChildItem src\monkey_logs -Filter summary_*.json -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```
