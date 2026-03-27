"""Persistent task state tracker for the agent daemon.

Maintains a JSON file of in-flight tasks so the daemon can detect orphaned
work on startup. Each task transitions through:

    accepted → dispatched → completed | failed | timed_out

If the daemon crashes or restarts, any tasks still in 'accepted' or
'dispatched' state are considered orphaned and reported to GroupMe.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_STATE_FILE = Path(__file__).resolve().parent.parent / "monkey_logs" / "pending_tasks.json"


def _read_state() -> dict[str, dict[str, Any]]:
    """Load the pending tasks map from disk."""
    if not TASK_STATE_FILE.is_file():
        return {}
    try:
        text = TASK_STATE_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomically write the pending tasks map to disk."""
    TASK_STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = TASK_STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # Atomic rename (on Windows this replaces if target exists on Python 3.12+,
        # but we handle the older case too)
        try:
            tmp.replace(TASK_STATE_FILE)
        except OSError:
            # Fallback for older Windows Python: remove then rename
            TASK_STATE_FILE.unlink(missing_ok=True)
            tmp.rename(TASK_STATE_FILE)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def track_accepted(message_id: str, sender: str, instruction: str) -> None:
    """Record that a task has been accepted from the queue."""
    state = _read_state()
    state[message_id] = {
        "status": "accepted",
        "sender": sender,
        "instruction": instruction[:500],
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_state(state)


def track_dispatched(message_id: str) -> None:
    """Record that a task has been dispatched to copilot."""
    state = _read_state()
    if message_id in state:
        state[message_id]["status"] = "dispatched"
        state[message_id]["dispatched_at"] = datetime.now(timezone.utc).isoformat()
    else:
        state[message_id] = {
            "status": "dispatched",
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        }
    _write_state(state)


def track_completed(message_id: str) -> None:
    """Remove a task from tracking after successful completion."""
    state = _read_state()
    state.pop(message_id, None)
    _write_state(state)


def track_failed(message_id: str, reason: str) -> None:
    """Remove a task from tracking after failure (caller handles reporting)."""
    state = _read_state()
    state.pop(message_id, None)
    _write_state(state)


def get_orphaned_tasks() -> list[dict[str, Any]]:
    """Return all tasks that are still pending (not completed/failed).

    Called on daemon startup to find work that was interrupted.
    """
    state = _read_state()
    orphaned = []
    for msg_id, info in state.items():
        info_copy = dict(info)
        info_copy["message_id"] = msg_id
        orphaned.append(info_copy)
    return orphaned


def clear_orphaned(message_id: str) -> None:
    """Remove a specific orphaned task after it has been reported."""
    state = _read_state()
    state.pop(message_id, None)
    _write_state(state)


def clear_all_orphaned() -> None:
    """Remove all orphaned tasks (called after reporting them)."""
    _write_state({})
