"""
StressBot Dashboard — FastAPI backend.

Serves a real-time monitoring dashboard for the TerminalStress project.
Provides REST endpoints for stress test data and SSE for live log streaming.

Usage:
    python src/dashboard/server.py
    # or
    uvicorn src.dashboard.server:app --reload --port 8420
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import glob as globmod
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import aiofiles
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "src" / "monkey_logs"
CRASHDUMP_DIR = REPO_ROOT / "crashdumps"
ANALYSIS_DIR = CRASHDUMP_DIR / ".analysis"
DASHBOARD_DIR = Path(__file__).resolve().parent

app = FastAPI(title="StressBot Dashboard", version="1.0.0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path, tail: int = 200) -> list[dict]:
    """Read last N lines of a JSONL file."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    result = []
    for line in lines[-tail:]:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def _read_json_safe(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def _file_mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# API: Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    """Overall system status."""
    daemon_log = _read_jsonl(LOG_DIR / "daemon.jsonl", tail=50)

    # Find last daemon_start
    last_start = None
    for entry in reversed(daemon_log):
        if entry.get("event") == "daemon_start":
            last_start = entry
            break

    # Count events
    dispatches = [e for e in daemon_log if e.get("event") == "dispatch"]
    completions = [e for e in daemon_log if e.get("event") == "completed"]
    errors = [e for e in daemon_log if e.get("event") in ("error", "timeout")]

    # Service logs
    service_stderr = LOG_DIR / "service_stderr.log"
    service_running = service_stderr.exists()

    return {
        "daemon": {
            "last_start": last_start,
            "recent_dispatches": len(dispatches),
            "recent_completions": len(completions),
            "recent_errors": len(errors),
        },
        "service": {
            "log_exists": service_running,
            "last_modified": _file_mtime_iso(service_stderr) if service_running else None,
        },
        "summary_count": len(list(LOG_DIR.glob("summary_*.json"))),
        "crash_dump_count": len(list(CRASHDUMP_DIR.glob("*.dmp"))) if CRASHDUMP_DIR.exists() else 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# API: Daemon log
# ---------------------------------------------------------------------------

@app.get("/api/daemon-log")
async def get_daemon_log(tail: int = 500):
    """Return recent daemon log entries."""
    entries = _read_jsonl(LOG_DIR / "daemon.jsonl", tail=tail)
    return {"entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# API: Live log stream (SSE)
# ---------------------------------------------------------------------------

async def _tail_file(path: Path, poll_interval: float = 1.0) -> AsyncGenerator[str, None]:
    """Yield new lines from a file as they appear."""
    if not path.exists():
        yield f"data: {json.dumps({'event': 'waiting', 'message': 'Log file not yet created'})}\n\n"
        while not path.exists():
            await asyncio.sleep(poll_interval)

    async with aiofiles.open(path, mode="r", encoding="utf-8", errors="replace") as f:
        # Seek to end
        await f.seek(0, 2)
        while True:
            line = await f.readline()
            if line:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        yield f"data: {json.dumps(entry)}\n\n"
                    except json.JSONDecodeError:
                        yield f"data: {json.dumps({'event': 'raw', 'message': line})}\n\n"
            else:
                await asyncio.sleep(poll_interval)


@app.get("/api/daemon-log/stream")
async def stream_daemon_log(request: Request):
    """SSE endpoint for live daemon log streaming."""
    async def event_generator():
        async for data in _tail_file(LOG_DIR / "daemon.jsonl"):
            if await request.is_disconnected():
                break
            yield data

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API: Stress test summaries
# ---------------------------------------------------------------------------

@app.get("/api/summaries")
async def get_summaries():
    """Return all stress test summary files."""
    files = sorted(LOG_DIR.glob("summary_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    summaries = []
    for f in files[:50]:
        data = _read_json_safe(f)
        if data:
            data["_file"] = f.name
            data["_mtime"] = _file_mtime_iso(f)
            summaries.append(data)
    return {"summaries": summaries, "count": len(summaries)}


# ---------------------------------------------------------------------------
# API: Campaigns
# ---------------------------------------------------------------------------

@app.get("/api/campaigns")
async def get_campaigns():
    """Return all campaign tracking files."""
    files = sorted(LOG_DIR.glob("campaign_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    campaigns = []
    for f in files[:30]:
        data = _read_json_safe(f)
        if data:
            data["_file"] = f.name
            data["_mtime"] = _file_mtime_iso(f)
            campaigns.append(data)
    return {"campaigns": campaigns, "count": len(campaigns)}


# ---------------------------------------------------------------------------
# API: Crash analysis
# ---------------------------------------------------------------------------

@app.get("/api/crashes")
async def get_crashes():
    """Return crash dump info and analysis summaries."""
    dumps = []
    if CRASHDUMP_DIR.exists():
        for f in sorted(CRASHDUMP_DIR.glob("*.dmp"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            dumps.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                "mtime": _file_mtime_iso(f),
            })

    analyses = []
    if ANALYSIS_DIR.exists():
        for f in sorted(ANALYSIS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            data = _read_json_safe(f)
            if data and isinstance(data, dict):
                data["_file"] = f.name
                analyses.append(data)

    return {"dumps": dumps, "analyses": analyses}


# ---------------------------------------------------------------------------
# API: Directives (from daemon log)
# ---------------------------------------------------------------------------

@app.get("/api/directives")
async def get_directives():
    """Extract directive lifecycle from daemon log."""
    entries = _read_jsonl(LOG_DIR / "daemon.jsonl", tail=1000)

    directives: dict[str, dict] = {}
    for e in entries:
        mid = e.get("message_id")
        if not mid:
            continue

        ev = e.get("event")
        if ev == "dispatch":
            directives[mid] = {
                "message_id": mid,
                "sender": e.get("sender", ""),
                "instruction": e.get("instruction", ""),
                "dispatched_at": e.get("timestamp", ""),
                "status": "running",
                "elapsed_seconds": None,
                "reply_chars": None,
                "exit_code": None,
            }
        elif ev == "completed" and mid in directives:
            directives[mid]["status"] = "completed"
            directives[mid]["elapsed_seconds"] = e.get("elapsed_seconds")
            directives[mid]["reply_chars"] = e.get("reply_chars")
            directives[mid]["exit_code"] = e.get("exit_code")
            directives[mid]["completed_at"] = e.get("timestamp", "")
        elif ev == "reply_posted" and mid in directives:
            directives[mid]["status"] = "replied"
            directives[mid]["replied_at"] = e.get("timestamp", "")
        elif ev == "timeout" and mid in directives:
            directives[mid]["status"] = "timeout"
            directives[mid]["elapsed_seconds"] = e.get("elapsed_seconds")
        elif ev == "error" and mid in directives:
            directives[mid]["status"] = "error"
            directives[mid]["error"] = e.get("error", "")
        elif ev == "quick_handle":
            directives[mid] = {
                "message_id": mid,
                "sender": e.get("sender", ""),
                "instruction": e.get("instruction", ""),
                "dispatched_at": e.get("timestamp", ""),
                "status": "quick_handled",
            }

    result = list(directives.values())
    result.reverse()
    return {"directives": result, "count": len(result)}


# ---------------------------------------------------------------------------
# API: Reply content (copilot monologue)
# ---------------------------------------------------------------------------

@app.get("/api/replies")
async def get_replies():
    """Return available reply file contents."""
    files = sorted(LOG_DIR.glob("reply_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    replies = []
    for f in files[:20]:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            # Extract message_id from filename: reply_<id>.txt
            mid = f.stem.replace("reply_", "")
            replies.append({
                "message_id": mid,
                "content": content[:5000],
                "chars": len(content),
                "mtime": _file_mtime_iso(f),
            })
        except OSError:
            continue
    return {"replies": replies, "count": len(replies)}


# ---------------------------------------------------------------------------
# API: Service logs
# ---------------------------------------------------------------------------

@app.get("/api/service-logs")
async def get_service_logs(tail: int = 100):
    """Return recent service stdout/stderr logs."""
    result = {}
    for name in ("service_stdout.log", "service_stderr.log"):
        path = LOG_DIR / name
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            result[name] = lines[-tail:]
        else:
            result[name] = []
    return result


# ---------------------------------------------------------------------------
# API: Monkey test logs
# ---------------------------------------------------------------------------

@app.get("/api/monkey-logs")
async def get_monkey_logs():
    """List available monkey test log files."""
    files = sorted(LOG_DIR.glob("monkey_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "logs": [
            {"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1), "mtime": _file_mtime_iso(f)}
            for f in files[:20]
        ]
    }


@app.get("/api/monkey-logs/{filename}")
async def get_monkey_log_content(filename: str, tail: int = 200):
    """Return tail of a specific monkey log."""
    if not re.match(r'^monkey_[\w\-]+\.log$', filename):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    path = LOG_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return {"filename": filename, "lines": lines[-tail:], "total_lines": len(lines)}


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = DASHBOARD_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
async def favicon():
    # Return a transparent 1x1 ICO to avoid 404s
    return JSONResponse(content={}, status_code=204)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 StressBot Dashboard starting on http://localhost:8420")
    print(f"   Log dir: {LOG_DIR}")
    print(f"   Crash dir: {CRASHDUMP_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8420, log_level="info")
