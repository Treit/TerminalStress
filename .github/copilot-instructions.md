# Copilot Instructions

## Build and Run

```bash
# Build
dotnet build src/TerminalStress.sln

# Run (UTF-8 mode, default)
dotnet run --project src/TerminalStress.csproj

# Run (UTF-7 mode, triggered by passing any argument)
dotnet run --project src/TerminalStress.csproj -- anyarg
```

## Architecture

Single-file C# console app (`src/Program.cs`) that stress tests Windows Terminal by running an infinite loop that:

- Randomly positions the cursor and writes random Unicode characters in random console colors
- Periodically clears the screen and dumps accumulated output
- Periodically floods the console with emoji sequences
- Swallows exceptions from invalid cursor positions or write failures and renders emoji error indicators instead

Passing any command-line argument switches the output encoding from UTF-8 to UTF-7.

## Conventions

- Target framework is .NET 7.0 (`net7.0`).
- `#pragma warning disable SYSLIB0001` is used intentionally to allow UTF-7 encoding for stress testing purposes.
- The solution file lives inside `src/` alongside the project and source files.
- Use `uv` instead of `pip` for installing Python packages (e.g., `uv pip install` instead of `pip install`).
- When creating or editing GitHub PRs with `gh` on PowerShell, always use `--body-file` instead of `--body` to avoid backtick escape corruption (PowerShell treats `` ` `` as an escape character, mangling markdown code spans).
- Always launch the monkey stress tester via `src\monkey\run_monkey.cmd` (which opens a visible `conhost.exe` window), never inline in the current shell. Forward all arguments: `src\monkey\run_monkey.cmd --duration 600 --launch --action-profile buffer-chaos`.

## GroupMe Notifications

When you discover a **new crashing bug** or noteworthy finding during stress testing, post a summary to the team GroupMe channel using the notification helper:

```python
# From Python
from monkey.notify_groupme import post
post("🐛 New crash: Pane::_GetMinSize null deref during resize (PID 64572)")

# From the command line
python src/monkey/notify_groupme.py "🐛 New crash: TextBuffer::GetSize AV during SelectAll"
```

**Setup:** The bot ID is read from the `GROUPME_BOT_ID` environment variable or a `.env` file in the repo root:
```
# .env (do NOT commit this file — it is gitignored)
GROUPME_BOT_ID=your_bot_id_here
```

**When to post:** Post when you find a new unique crash signature, a new hang bucket, or a reproduction of a known bug with new details. Keep messages concise — include the crash function, exception type, and what triggered it.
