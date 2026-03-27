"""Daemon that polls the GroupMe agent inbox and dispatches directives.

Runs as a persistent background process. When a directed message arrives
in the Azure Storage Queue, this daemon:

  1. Launches `copilot -p "<directive>" --yolo --autopilot` in the repo
     directory so a full Copilot CLI agent carries out the work
  2. Reads the spawned agent's reply text from `src/monkey_logs/reply_*.txt`
     and posts it to GroupMe
  3. Logs every dispatch to src/monkey_logs/daemon.jsonl for visibility

Usage:
    # Start the daemon (runs until Ctrl+C)
    python src/monkey/agent_daemon.py

    # Custom interval
    python src/monkey/agent_daemon.py --interval 15

    # Dry run — print directives without launching copilot
    python src/monkey/agent_daemon.py --dry-run
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# In Session 0 (Windows service), stdout/stderr default to cp1252 which can't
# encode emoji.  Replace un-encodable chars instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

# Allow importing sibling modules
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from monkey.agent_inbox import _get_config, get_all_directives
from monkey.notify_groupme import post as groupme_post

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "src" / "monkey_logs"
LOG_FILE = LOG_DIR / "daemon.jsonl"


def _find_copilot() -> str | None:
    """Find the GitHub Copilot CLI executable.

    Prefers the WinGet-installed binary over the Microsoft Copilot UWP app
    (which lives in WindowsApps and is NOT the CLI). Falls back to PATH.
    """
    # Prefer the known WinGet install location
    winget_copilot = Path(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Links", "copilot.exe",
    )
    if winget_copilot.is_file():
        return str(winget_copilot)

    # Fallback: search PATH, but skip the WindowsApps Microsoft Copilot stub
    found = shutil.which("copilot")
    if found and "WindowsApps" not in found:
        return found

    # Last resort: check the WinGet packages dir directly
    winget_pkg = Path(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages",
    )
    if winget_pkg.is_dir():
        for candidate in winget_pkg.glob("GitHub.Copilot_*/copilot.exe"):
            return str(candidate)

    return found  # May be None or the WindowsApps stub


def _is_session_zero() -> bool:
    """Return True if running in Session 0 (Windows service context).

    In Session 0, conhost.exe cannot create interactive console windows and
    exits immediately — so we must launch powershell/copilot directly.
    """
    try:
        pid = ctypes.windll.kernel32.GetCurrentProcessId()
        session_id = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
            return session_id.value == 0
    except (AttributeError, OSError):
        pass
    return False


def _log_entry(entry: dict) -> None:
    """Append a JSON log entry to daemon.jsonl."""
    LOG_DIR.mkdir(exist_ok=True)
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _powershell_literal(value: str) -> str:
    """Return a single-quoted PowerShell literal."""
    return value.replace("'", "''")


def _consume_reply_file(reply_file: Path) -> str | None:
    """Read and delete a reply file if present."""
    if not reply_file.is_file():
        return None

    try:
        text = reply_file.read_text(encoding="utf-8").strip()
        return text or None
    finally:
        reply_file.unlink(missing_ok=True)


def _try_quick_handle(instruction: str, safety_override: bool = False) -> str | None:
    """Handle trivially simple directives without spawning copilot.

    Returns a response string if handled, or None to escalate to copilot.
    """
    cmd = instruction.lower().strip()

    if cmd in ("ping", "hello", "hi"):
        return "🤖 pong!"

    if cmd in ("status", "health"):
        return "🤖 stressbot daemon is online and listening."

    if cmd.startswith("help"):
        return (
            "🤖 Send me any instruction in plain English and I'll carry it out.\n"
            "Quick responses: ping, status, help, time\n"
            "Everything else spawns a full Copilot agent session (~30-60s).\n"
            "Prefix with @@! to override safety checks."
        )

    # Only match exact "time" queries, not "uptime", "runtime", etc.
    _time_phrases = [
        "what time is it", "what's the time", "current time",
        "time now", "the time", "tell me the time",
    ]
    if any(cmd == p or cmd.startswith(p + "?") or cmd.endswith(p) for p in _time_phrases):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        return f"🤖 {now}"

    # Safety: refuse requests that look like secret/credential exfiltration
    # @@! prefix overrides this check (developer override)
    if not safety_override:
        _sensitive_patterns = [
            ".env", "secret", "password", "api_key", "api key", "apikey",
            "token", "credential", "connection_string", "connectionstring",
            "private key", "privatekey",
        ]
        if any(p in cmd for p in _sensitive_patterns):
            return "🤖 Nice try! I can't share secrets, keys, or credentials. 🔒 (Use @@! to override)"

    return None  # Not a quick command — escalate to copilot


def _dispatch_directive(directive: dict, copilot_path: str, dry_run: bool = False) -> None:
    """Handle a directive — quick-path if trivial, otherwise spawn copilot."""
    instruction = directive["instruction"]
    sender = directive["sender_name"]
    message_id = directive.get("message_id", "unknown")
    raw_text = directive.get("raw_text", "")
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', message_id)[:40]
    prompt_file = LOG_DIR / f"prompt_{safe_id}.txt"
    reply_rel_path = f"src\\monkey_logs\\reply_{safe_id}.txt"
    reply_file = LOG_DIR / f"reply_{safe_id}.txt"

    # @@! prefix = safety override from the developer
    safety_override = raw_text.strip().startswith("@@!")

    print(f"  Dispatching: {instruction}")

    # Try quick handling first (< 1 second, no copilot spawn)
    quick_response = _try_quick_handle(instruction, safety_override=safety_override)
    if quick_response is not None:
        print(f"  Quick response ({len(quick_response)} chars)")
        _log_entry({
            "event": "quick_handle",
            "sender": sender,
            "instruction": instruction,
            "message_id": message_id,
        })
        if not dry_run:
            groupme_post(quick_response)
        else:
            print(f"  [dry-run] Would post: {quick_response[:80]}")
        return

    # Complex directive — spawn a full copilot session
    _log_entry({
        "event": "dispatch",
        "sender": sender,
        "instruction": instruction,
        "message_id": message_id,
        "dry_run": dry_run,
    })

    # Build the prompt — .github/copilot-instructions.md provides full context
    prompt = (
        f"Directive from {sender} via GroupMe:\n\n"
        f"{instruction}\n\n"
        f"CRITICAL INSTRUCTIONS:\n"
        f"1. Carry out the request above. Run commands, look things up, do whatever is needed.\n"
        f"2. You MUST write a useful, substantive answer to the file below.\n"
        f"3. NEVER write just 'Done' or 'Task completed' — always include the actual result/data.\n"
        f"4. If the request is unclear, make your best interpretation and answer that.\n"
        f"5. Focus ONLY on the current request. Ignore references to prior conversations.\n\n"
        f"Write your final reply (plain text, one message) to this UTF-8 file:\n"
        f"{reply_rel_path}\n\n"
        f"Do NOT post directly to GroupMe; the daemon posts from this file."
    )

    if dry_run:
        print(f"  [dry-run] Would launch: copilot -p \"{instruction[:60]}...\"")
        return

    start = time.time()
    try:
        # Clean env — uv run injects NODE_OPTIONS=--no-warnings which breaks copilot
        clean_env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}

        LOG_DIR.mkdir(exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        # Read prompt text from disk to avoid shell escaping/expansion issues.
        powershell_command = (
            f"$p=Get-Content -LiteralPath '{_powershell_literal(str(prompt_file))}' "
            f"-Raw -Encoding UTF8; "
            f"Set-Location -LiteralPath '{_powershell_literal(str(REPO_ROOT))}'; "
            f"& '{_powershell_literal(copilot_path)}' --yolo --autopilot -p $p"
        )

        # Launch copilot. In Session 0 (service context), conhost.exe can't
        # create interactive windows and exits immediately, so run directly.
        in_session_zero = _is_session_zero()

        if in_session_zero:
            # Direct launch — no conhost wrapper
            # Capture stderr for diagnostics, discard stdout
            stderr_path = LOG_DIR / f"copilot_stderr_{safe_id}.log"
            stderr_file = open(stderr_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [
                    "powershell",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell_command,
                ],
                env=clean_env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            # Interactive launch — conhost wrapper (survives WT crashes)
            proc = subprocess.Popen(
                [
                    "conhost.exe",
                    "powershell",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell_command,
                ],
                env=clean_env,
            )

        try:
            proc.wait(timeout=3600)  # 60 minute max
        except subprocess.TimeoutExpired:
            proc.kill()
            elapsed = round(time.time() - start, 1)
            print(f"  Timed out after {elapsed}s")
            _log_entry({"event": "timeout", "message_id": message_id, "elapsed_seconds": elapsed})
            groupme_post("🤖 Timed out (60 min limit) — directive was too complex for one shot.")
            return

        elapsed = round(time.time() - start, 1)
        reply_text = _consume_reply_file(reply_file)

        _log_entry({
            "event": "completed",
            "message_id": message_id,
            "exit_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "instruction": instruction[:200],
            "reply_file": reply_rel_path,
            "reply_chars": len(reply_text) if reply_text else 0,
        })

        if proc.returncode == 0:
            print(f"  Completed in {elapsed}s (exit 0)")
            if reply_text:
                ok = groupme_post(reply_text)
                if ok:
                    print(f"  Posted reply ({len(reply_text)} chars)")
                    _log_entry({
                        "event": "reply_posted",
                        "message_id": message_id,
                        "chars": len(reply_text),
                    })
                else:
                    print("  warning: reply post failed")
                    _log_entry({"event": "reply_post_failed", "message_id": message_id})
            else:
                groupme_post("🤖 Done — task completed.")
                _log_entry({"event": "reply_missing", "message_id": message_id})
        else:
            print(f"  Failed in {elapsed}s (exit {proc.returncode})")
            groupme_post(f"🤖 Hit an issue (exit code {proc.returncode})")

    except Exception as exc:
        print(f"  Error: {exc}")
        _log_entry({"event": "error", "message_id": message_id, "error": str(exc)})
        groupme_post(f"🤖 Error: {exc}")
    finally:
        prompt_file.unlink(missing_ok=True)
        # Close stderr capture file if it was opened
        if in_session_zero and 'stderr_file' in dir():
            try:
                stderr_file.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daemon: poll GroupMe inbox and dispatch to Copilot CLI.",
    )
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="Polling interval in seconds (default: 10).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print directives without launching copilot.",
    )
    args = parser.parse_args()

    # Verify copilot CLI is available
    copilot_path = _find_copilot()
    if not copilot_path and not args.dry_run:
        print("error: 'copilot' CLI not found on PATH.", file=sys.stderr)
        sys.exit(1)

    config = _get_config()
    session_zero = _is_session_zero()
    print(f"Agent daemon started")
    print(f"  Agent: {config['agent_name']}")
    print(f"  Queue: {config['queue_name']}")
    print(f"  Log:   {LOG_FILE}")
    print(f"  Copilot: {copilot_path or '(dry-run)'}")
    print(f"  Interval: {args.interval}s")
    print(f"  Repo: {REPO_ROOT}")
    print(f"  Session 0: {session_zero}")
    if session_zero:
        print(f"  Mode: headless (service) — conhost disabled")
    print()

    _log_entry({
        "event": "daemon_start",
        "agent": config["agent_name"],
        "interval": args.interval,
        "session_zero": session_zero,
    })

    try:
        while True:
            directives = get_all_directives(config)
            for d in directives:
                _dispatch_directive(d, copilot_path, dry_run=args.dry_run)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _log_entry({"event": "daemon_stop"})
        print("\nDaemon stopped.")


if __name__ == "__main__":
    main()
