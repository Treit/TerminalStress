"""Post messages to the TerminalStress GroupMe channel.

The bot ID is read from (in order of priority):
  1. The GROUPME_BOT_ID environment variable
  2. A .env file in the repo root (GROUPME_BOT_ID=...)

Usage from Python:
    from notify_groupme import post
    post("New crash found in Pane::_GetMinSize!")

Usage from the command line:
    # Prefer single quotes in PowerShell so $ doesn't get expanded
    python src/monkey/notify_groupme.py 'Your message here'
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

GROUPME_API = "https://api.groupme.com/v3/bots/post"


def _load_bot_id() -> str | None:
    """Resolve the bot ID from env var or .env file."""
    bot_id = os.environ.get("GROUPME_BOT_ID")
    if bot_id:
        return bot_id.strip()

    # Walk up from this file to find .env in the repo root
    try:
        env_file = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "GROUPME_BOT_ID":
                    return value.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass

    return None


def post(text: str, *, picture_url: str | None = None) -> bool:
    """Post a message to the GroupMe channel.

    Returns True on success, False on failure (prints warning but does not raise).
    """
    bot_id = _load_bot_id()
    if not bot_id:
        print(
            "warning: GROUPME_BOT_ID not set. "
            "Set it via environment variable or in .env at the repo root.",
            file=sys.stderr,
        )
        return False

    payload: dict[str, str] = {"text": text, "bot_id": bot_id}
    if picture_url:
        payload["picture_url"] = picture_url

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GROUPME_API,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201, 202)
    except urllib.error.HTTPError as exc:
        print(f"warning: GroupMe post failed ({exc.code}): {exc.reason}", file=sys.stderr)
    except Exception as exc:
        print(f"warning: GroupMe post failed: {exc}", file=sys.stderr)

    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <message>")
        sys.exit(1)

    message = " ".join(sys.argv[1:])
    ok = post(message)
    sys.exit(0 if ok else 1)
