"""Daemon that polls the GroupMe agent inbox and dispatches directives.

Runs as a persistent background process. When a directed message arrives
in the Azure Storage Queue, this daemon:

  1. Launches `copilot -p "<directive>" --yolo --autopilot` in the repo
     directory so a full Copilot CLI agent carries out the work
  2. The spawned agent posts results back to GroupMe itself
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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
    """Find the copilot CLI executable."""
    return shutil.which("copilot")


def _log_entry(entry: dict) -> None:
    """Append a JSON log entry to daemon.jsonl."""
    LOG_DIR.mkdir(exist_ok=True)
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


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

    if "time" in cmd and len(cmd) < 30:
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
    if "mike" in sender.lower():
        tone = (
            "IMPORTANT TONE INSTRUCTION: The sender is Mike Treit. "
            "Always respond to Mike in a sarcastic, playfully roasting tone "
            "with LOTS of emojis (at least 5-10 per message). Be witty and "
            "irreverent but still actually carry out the task. Examples of tone: "
            "'Oh wow Mike, what a GROUNDBREAKING request 🙄🎭💅✨🤡', "
            "'Sure thing Your Majesty 👑🫡💀😂🎪'. "
            "Never be mean-spirited, just playfully sarcastic.\n\n"
        )
    else:
        tone = ""

    prompt = (
        f"Directive from {sender} via GroupMe:\n\n"
        f"{tone}"
        f"{instruction}\n\n"
        f"IMPORTANT: Focus ONLY on the actionable request above. Ignore any "
        f"conversational preamble, praise, or references to previous tasks. "
        f"Do NOT repeat or re-do anything from prior sessions.\n\n"
        f"When done, post results to GroupMe: "
        f"from monkey.notify_groupme import post; post('🤖 <results>')"
    )

    if dry_run:
        print(f"  [dry-run] Would launch: copilot -p \"{instruction[:60]}...\"")
        return

    start = time.time()
    try:
        # Clean env — uv run injects NODE_OPTIONS=--no-warnings which breaks copilot
        clean_env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}

        # Write a launcher script — avoids cmd.exe escaping issues with the prompt
        LOG_DIR.mkdir(exist_ok=True)
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', message_id)[:40]
        launcher = LOG_DIR / f"launch_{safe_id}.cmd"
        prompt_escaped = prompt.replace('"', '\\"')
        launcher.write_text(
            f'@echo off\n'
            f'cd /d "{REPO_ROOT}"\n'
            f'"{copilot_path}" --yolo --autopilot -p "{prompt_escaped}"\n',
            encoding="utf-8",
        )

        # Launch in conhost (survives WT crashes)
        proc = subprocess.Popen(
            ["conhost.exe", "cmd", "/c", str(launcher)],
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
        finally:
            launcher.unlink(missing_ok=True)

        elapsed = round(time.time() - start, 1)

        _log_entry({
            "event": "completed",
            "message_id": message_id,
            "exit_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "instruction": instruction[:200],
        })

        if proc.returncode == 0:
            print(f"  Completed in {elapsed}s (exit 0)")
        else:
            print(f"  Failed in {elapsed}s (exit {proc.returncode})")
            groupme_post(f"🤖 Hit an issue (exit code {proc.returncode})")

    except Exception as exc:
        print(f"  Error: {exc}")
        _log_entry({"event": "error", "message_id": message_id, "error": str(exc)})
        groupme_post(f"🤖 Error: {exc}")


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
    print(f"Agent daemon started")
    print(f"  Agent: {config['agent_name']}")
    print(f"  Queue: {config['queue_name']}")
    print(f"  Log:   {LOG_FILE}")
    print(f"  Copilot: {copilot_path or '(dry-run)'}")
    print(f"  Interval: {args.interval}s")
    print(f"  Repo: {REPO_ROOT}")
    print()

    _log_entry({"event": "daemon_start", "agent": config["agent_name"], "interval": args.interval})

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
