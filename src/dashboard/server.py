"""
TerminalStress Dashboard -- FastAPI backend.

Serves a monkey- and crash-focused dashboard for the TerminalStress project.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = REPO_ROOT / "src" / "monkey_logs"
CRASHDUMP_DIR = REPO_ROOT / "crashdumps"
ANALYSIS_DIR = CRASHDUMP_DIR / ".analysis"
DASHBOARD_DIR = Path(__file__).resolve().parent

app = FastAPI(title="TerminalStress Dashboard", version="2.0.0")


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


def _sorted_files(pattern: str, root: Path, limit: int) -> list[Path]:
    return sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


@app.get("/api/status")
async def get_status():
    """Return dashboard status counters."""
    summaries = _sorted_files("summary_*.json", LOG_DIR, limit=1)
    latest_summary = _read_json_safe(summaries[0]) if summaries else None
    if latest_summary is not None:
        latest_summary["_mtime"] = _file_mtime_iso(summaries[0])

    return {
        "summary_count": len(list(LOG_DIR.glob("summary_*.json"))),
        "campaign_count": len(list(LOG_DIR.glob("campaign_*.json"))),
        "crash_dump_count": len(list(CRASHDUMP_DIR.glob("*.dmp"))) if CRASHDUMP_DIR.exists() else 0,
        "analysis_count": len(list(ANALYSIS_DIR.glob("*.json"))) if ANALYSIS_DIR.exists() else 0,
        "monkey_log_count": len(list(LOG_DIR.glob("monkey_*.log"))),
        "latest_summary": latest_summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/summaries")
async def get_summaries():
    """Return recent stress test summaries."""
    files = _sorted_files("summary_*.json", LOG_DIR, limit=50)
    summaries = []
    for path in files:
        data = _read_json_safe(path)
        if data:
            data["_file"] = path.name
            data["_mtime"] = _file_mtime_iso(path)
            summaries.append(data)
    return {"summaries": summaries, "count": len(summaries)}


@app.get("/api/campaigns")
async def get_campaigns():
    """Return recent campaign summaries."""
    files = _sorted_files("campaign_*.json", LOG_DIR, limit=30)
    campaigns = []
    for path in files:
        data = _read_json_safe(path)
        if data:
            data["_file"] = path.name
            data["_mtime"] = _file_mtime_iso(path)
            campaigns.append(data)
    return {"campaigns": campaigns, "count": len(campaigns)}


@app.get("/api/crashes")
async def get_crashes():
    """Return crash dump inventory and analysis summaries."""
    dumps = []
    if CRASHDUMP_DIR.exists():
        for path in _sorted_files("*.dmp", CRASHDUMP_DIR, limit=30):
            dumps.append({
                "name": path.name,
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "mtime": _file_mtime_iso(path),
            })

    analyses = []
    if ANALYSIS_DIR.exists():
        for path in _sorted_files("*.json", ANALYSIS_DIR, limit=30):
            data = _read_json_safe(path)
            if isinstance(data, dict):
                data["_file"] = path.name
                data["_mtime"] = _file_mtime_iso(path)
                analyses.append(data)

    return {"dumps": dumps, "analyses": analyses}


@app.get("/api/monkey-logs")
async def get_monkey_logs():
    """List recent monkey log files."""
    files = _sorted_files("monkey_*.log", LOG_DIR, limit=20)
    return {
        "logs": [
            {
                "name": path.name,
                "size_kb": round(path.stat().st_size / 1024, 1),
                "mtime": _file_mtime_iso(path),
            }
            for path in files
        ]
    }


@app.get("/api/monkey-logs/{filename}")
async def get_monkey_log_content(filename: str, tail: int = 200):
    """Return the tail of a specific monkey log."""
    if not re.match(r"^monkey_[\w\-]+\.log$", filename):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    path = LOG_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)

    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return {"filename": filename, "lines": lines[-tail:], "total_lines": len(lines)}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((DASHBOARD_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(content={}, status_code=204)


if __name__ == "__main__":
    import uvicorn

    print("TerminalStress Dashboard starting on http://localhost:8420")
    print(f"  Log dir:   {LOG_DIR}")
    print(f"  Crash dir: {CRASHDUMP_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=8420, log_level="info")
