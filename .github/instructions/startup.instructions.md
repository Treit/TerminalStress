# Session Startup

At the **very start of every session**, before doing anything else, run:

```powershell
.\src\monkey\ensure-daemon.ps1
```

This is now a guard script only. It intentionally does **not** start the
retired in-repo daemon (`src\monkey\agent_daemon.py`), because the standalone
`agentinbox` project owns background inbox processing now.

After running the startup script, check the inbox with `uv`:

```powershell
uv run python src/monkey/agent_inbox.py
```

Then proceed with whatever task the user requested.
