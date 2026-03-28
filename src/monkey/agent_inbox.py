"""Poll the GroupMe agent inbox queue and process directed messages.

The agent inbox is an Azure Storage Queue populated by a GroupMe webhook
callback. Messages arrive as JSON matching the groupme-directed-message/v1
schema. This module polls the queue, dispatches each instruction, posts
results back to GroupMe, and deletes the message on success.

Environment / .env variables used:
    STORAGE_CONNECTION_STRING  — Azure Storage connection string
    GROUPME_QUEUE_NAME         — queue name (default: groupme-agent-inbox)
    GROUPME_AGENT_NAME         — this agent's name (default: stressbot)
    GROUPME_BOT_ID             — bot ID for posting replies

Usage:
    # One-shot: process all pending messages and exit
    python src/monkey/agent_inbox.py

    # Poll continuously (check every 10s)
    python src/monkey/agent_inbox.py --poll --interval 10

    # Peek without consuming
    python src/monkey/agent_inbox.py --peek
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

# In Session 0 (Windows service), stdout/stderr default to cp1252 which can't
# encode emoji (e.g. 🤦‍♂️).  Replace un-encodable chars instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

try:
    from azure.storage.queue import QueueClient
    from azure.core.exceptions import ResourceNotFoundError
except ImportError:
    # Try to install automatically
    import subprocess as _sp
    print("azure-storage-queue not found — installing...", file=sys.stderr)
    _rc = _sp.call([sys.executable, "-m", "pip", "install", "-q", "azure-storage-queue"])
    if _rc == 0:
        from azure.storage.queue import QueueClient
        from azure.core.exceptions import ResourceNotFoundError
    else:
        print(
            f"error: azure-storage-queue is required. Install with:\n"
            f"  {sys.executable} -m pip install azure-storage-queue",
            file=sys.stderr,
        )
        sys.exit(1)

# Allow importing sibling modules when run directly
_MONKEY_DIR = Path(__file__).resolve().parent
if str(_MONKEY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_MONKEY_DIR.parent))

from monkey.notify_groupme import post as groupme_post

EXPECTED_SCHEMA = "groupme-directed-message/v1"


# ── .env loader ────────────────────────────────────────────────────────────
def _load_env() -> None:
    """Load variables from .env into os.environ (simple, no dependencies)."""
    try:
        env_file = Path(__file__).resolve().parent.parent.parent / ".env"
        if not env_file.is_file():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except (OSError, UnicodeDecodeError):
        pass


_load_env()


# ── Config ─────────────────────────────────────────────────────────────────
def _get_config() -> dict[str, str]:
    conn = os.environ.get("STORAGE_CONNECTION_STRING", "")
    if not conn:
        print("error: STORAGE_CONNECTION_STRING not set.", file=sys.stderr)
        sys.exit(1)
    return {
        "connection_string": conn,
        "queue_name": os.environ.get("GROUPME_QUEUE_NAME", "groupme-agent-inbox"),
        "agent_name": os.environ.get("GROUPME_AGENT_NAME", "stressbot"),
    }


# ── Message parsing ───────────────────────────────────────────────────────
def _parse_queue_message(raw_content: str) -> dict | None:
    """Parse a queue message, handling both plain JSON and base64-encoded."""
    # Try plain JSON first
    try:
        msg = json.loads(raw_content)
        if isinstance(msg, dict):
            return msg
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: base64 decode then JSON
    try:
        decoded = base64.b64decode(raw_content).decode("utf-8")
        msg = json.loads(decoded)
        if isinstance(msg, dict):
            return msg
    except Exception:
        pass

    return None


def _validate_message(msg: dict, agent_name: str) -> str | None:
    """Validate schema and target. Returns error string or None if valid."""
    schema = msg.get("schema", "")
    if schema != EXPECTED_SCHEMA:
        return f"unexpected schema: {schema}"

    target = msg.get("targetAgent", "")
    if target and target != agent_name:
        return f"message for different agent: {target}"

    text = msg.get("message", {}).get("text", "")
    if not text.strip():
        return "empty message text"

    return None


def _extract_instruction(text: str) -> str:
    """Strip directive prefixes from the message text."""
    prefixes = ["@@!", "@@", "🤖", "@stressbot", "stressbot:", "/stressbot", "!stressbot"]
    stripped = text.strip()
    for prefix in prefixes:
        if stripped.lower().startswith(prefix.lower()):
            stripped = stripped[len(prefix):].strip()
            break
    return stripped


# ── Instruction handling ──────────────────────────────────────────────────
# Directives from the inbox are natural-language instructions from the
# developer.  The agent_inbox module does NOT try to interpret them —
# it acknowledges receipt in GroupMe and returns them to the caller so
# the agent (Copilot, a script, etc.) can carry out the request and
# post results back via notify_groupme.post().

def _acknowledge(instruction: str, sender_name: str) -> str:
    """Build a short, casual GroupMe acknowledgment."""
    return "🤖 On it — will post back with results."


# ── Queue operations ──────────────────────────────────────────────────────
def _get_queue_client(config: dict) -> QueueClient:
    return QueueClient.from_connection_string(
        config["connection_string"],
        queue_name=config["queue_name"],
    )


def _is_missing_queue_error(exc: Exception) -> bool:
    """Return True if the exception means the legacy queue does not exist."""
    if isinstance(exc, ResourceNotFoundError):
        return True
    text = str(exc)
    return (
        "QueueNotFound" in text
        or "specified queue does not exist" in text
        or "does not exist" in text
    )


def peek_messages(config: dict, max_messages: int = 5) -> list[dict]:
    """Peek at messages without consuming them."""
    client = _get_queue_client(config)
    peeked = client.peek_messages(max_messages=max_messages)
    results = []
    for msg in peeked:
        parsed = _parse_queue_message(msg.content)
        results.append({
            "id": msg.id,
            "parsed": parsed,
            "raw": msg.content[:200] if not parsed else None,
        })
    return results


def process_one(config: dict) -> dict | None:
    """Dequeue one message, acknowledge it, and return the directive.

    Returns a dict with {instruction, sender_name, message_id} if a
    message was processed, or None if the queue was empty.  The message
    is deleted from the queue after acknowledgment — the caller is
    responsible for carrying out the instruction and posting results
    back via notify_groupme.post().
    """
    client = _get_queue_client(config)
    messages = client.receive_messages(messages_per_page=1, visibility_timeout=60)

    for msg in messages:
        parsed = _parse_queue_message(msg.content)
        if parsed is None:
            print(f"  warning: unparseable message {msg.id}, skipping")
            client.delete_message(msg)
            continue

        error = _validate_message(parsed, config["agent_name"])
        if error:
            print(f"  skip: {error} (msg {msg.id})")
            client.delete_message(msg)
            continue

        sender = parsed.get("sender", {})
        sender_name = sender.get("name", "Unknown")
        message_id = parsed.get("source", {}).get("messageId", msg.id)
        text = parsed.get("message", {}).get("text", "")
        instruction = _extract_instruction(text)

        print(f"  [{sender_name}] {instruction}")

        # Brief ack so the sender knows it was received
        groupme_post("🫡")

        # Delete from queue — we've accepted responsibility.
        client.delete_message(msg)

        return {
            "instruction": instruction,
            "sender_name": sender_name,
            "message_id": message_id,
            "raw_text": text,
        }

    return None  # Queue empty


def get_all_directives(config: dict) -> list[dict]:
    """Dequeue and acknowledge all pending directives.

    Returns a list of {instruction, sender_name, message_id} dicts.
    Each message is acknowledged in GroupMe and deleted from the queue.
    The caller should carry out each instruction and post results.
    """
    directives = []
    while True:
        directive = process_one(config)
        if directive is None:
            break
        directives.append(directive)
    return directives


def poll_loop(config: dict, interval: float = 10.0) -> None:
    """Poll continuously, printing directives as they arrive."""
    print(f"Polling {config['queue_name']} every {interval}s (Ctrl+C to stop)...")
    try:
        while True:
            directives = get_all_directives(config)
            if directives:
                print(f"  processed {len(directives)} directive(s)")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll the GroupMe agent inbox and process directed messages.",
    )
    parser.add_argument(
        "--peek", action="store_true",
        help="Peek at messages without consuming them.",
    )
    parser.add_argument(
        "--poll", action="store_true",
        help="Poll continuously instead of one-shot.",
    )
    parser.add_argument(
        "--interval", type=float, default=10.0,
        help="Polling interval in seconds (default: 10).",
    )
    args = parser.parse_args()

    config = _get_config()
    print(f"Agent: {config['agent_name']}  Queue: {config['queue_name']}")

    try:
        if args.peek:
            messages = peek_messages(config)
            if not messages:
                print("Queue is empty.")
            else:
                print(f"{len(messages)} message(s):")
                for m in messages:
                    if m["parsed"]:
                        sender = m["parsed"].get("sender", {}).get("name", "?")
                        text = m["parsed"].get("message", {}).get("text", "")[:100]
                        print(f"  [{sender}] {text}")
                    else:
                        print(f"  [unparseable] {m['raw']}")
            return

        if args.poll:
            poll_loop(config, interval=args.interval)
        else:
            directives = get_all_directives(config)
            if not directives:
                print("No pending directives.")
            else:
                print(f"Received {len(directives)} directive(s):")
                for d in directives:
                    print(f"  [{d['sender_name']}] {d['instruction']}")
    except Exception as exc:
        if _is_missing_queue_error(exc):
            print(
                f"Legacy inbox queue '{config['queue_name']}' not found; "
                "skipping startup inbox check."
            )
            return
        raise


if __name__ == "__main__":
    main()
