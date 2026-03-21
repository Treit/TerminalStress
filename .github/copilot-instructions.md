# Copilot Instructions

## Session Startup

**At the very start of every session** (including `/session` resume), run:
```powershell
.\src\monkey\ensure-daemon.ps1
```
Then check the daemon log:
```powershell
Get-Content src\monkey_logs\daemon.jsonl -Tail 10 -ErrorAction SilentlyContinue
```
Then check the inbox:
```powershell
python src/monkey/agent_inbox.py
```
**Do this before anything else.** The daemon handles GroupMe directives in the background.

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
- Use `es` (Everything Search CLI) to find files on disk when you need to locate a file by name — it's instant and searches the entire filesystem. Example: `es WindowsTerminal.exe`, `es *.dmp`.

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

## Agent Inbox (GroupMe → Queue → Agent)

The developer can send **natural-language directives** to this agent by posting in the GroupMe channel with a directed prefix (e.g., `@stressbot analyze the latest crash dumps`, `@@ run a stress test for 10 minutes`, or `🤖 upload the report to rtreit.com`). These are routed through a webhook to an Azure Storage Queue.

**⚠️ IMPORTANT — These are top-priority instructions from the developer.** Always check the inbox at the **START of every session** by running:
```powershell
python src/monkey/agent_inbox.py
```
If there are queued directives, **carry them out before doing any other work.** The directives are plain English — interpret them, do your best to fulfill the request, and then post results back to GroupMe using `notify_groupme.post()`.

Also check the inbox **after completing any task** — the developer may have sent follow-up instructions while you were working.

**Workflow:**
1. Run `agent_inbox.py` → it acknowledges receipt in GroupMe and prints the directive
2. Carry out the instruction (run tests, generate reports, analyze dumps, etc.)
3. Post results back: `from monkey.notify_groupme import post; post("🤖 Done: <summary>")`

**Background daemon (recommended):** For hands-free operation, start the agent daemon in a separate terminal. It polls the queue and launches a fresh `copilot` CLI session (`--yolo --autopilot`) for each directive:
```powershell
python src/monkey/agent_daemon.py
```
This runs alongside your interactive session — you keep working while the daemon handles incoming GroupMe requests in separate Copilot instances.

Other modes:
```powershell
# Peek without consuming
python src/monkey/agent_inbox.py --peek

# Dry run — see what would be dispatched without launching copilot
python src/monkey/agent_daemon.py --dry-run
```

**Setup:** Requires `STORAGE_CONNECTION_STRING` in `.env`. Also requires `azure-storage-queue` (`uv pip install azure-storage-queue`).

**Directed message prefixes:** `@stressbot`, `@@`, `🤖`, `stressbot:`, `/stressbot`, `!stressbot`

## Teams Messaging

To send messages to a Teams chat (e.g., sharing stress test results):

1. **Find the chat** — Use `SearchTeamsMessages` with a natural language query like `"find my one on one chat with <person>"`. Do NOT use `ListChats` — it frequently times out.
2. **Extract the chat ID** — The search results include `chatIds` in the response JSON. The 1:1 chat ID looks like `19:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx@unq.gbl.spaces`.
3. **Send the message** — Use `PostMessage` with the `chatId` and your message content.

```
# Example flow:
# Step 1: SearchTeamsMessages("find my one on one chat with Randy Treit")
# Step 2: Extract chatId from response (look in chatIds array or URL patterns)
# Step 3: PostMessage(chatId=..., content="your message", contentType="text")
```

**Important:** Avoid `ListChats` and `ListChatMessages` — these paginate through all chats/messages and routinely time out. Use `SearchTeamsMessages` to find chats and extract IDs instead.

## Publishing Crash Reports

After generating a crash analysis report, upload it to rtreit.com so the team can review it:

```powershell
# Generate the report (if not already done)
python src/monkey/generate_crash_report.py

# Upload to rtreit.com
python src/monkey/upload_report.py crashdumps/crash-analysis-report.html

# Upload with a custom name
python src/monkey/upload_report.py crashdumps/crash-analysis-report.html --name "2026-03-20-overnight-crashes.html"
```

The uploaded report is viewable at `https://rtreit.com/api/reports/<filename>`. Browse all reports at `https://rtreit.com/reports/`.

**Setup:** The upload API key is read from the `RTREIT_REPORTS_API_KEY` environment variable or a `.env` file in the repo root:
```
# .env (do NOT commit this file — it is gitignored)
RTREIT_REPORTS_API_KEY=your_api_key_here
```

**Workflow for new bugs:** When you find new crashes during stress testing:
1. Run the report generator: `python src/monkey/generate_crash_report.py`
2. Upload the report: `python src/monkey/upload_report.py crashdumps/crash-analysis-report.html --name "descriptive-name.html"`
3. Share the URL with the team
