"""Generate an HTML crash-analysis report from Windows Terminal dump files.

This script automates the full pipeline:
  1. Locate cdb.exe (WinDbg / Windows SDK debugger).
  2. Run cdb analysis on each .dmp file in the crashdumps directory.
  3. Parse the analysis logs and generate a single self-contained HTML report.

Usage examples:
    # Full pipeline — analyse dumps and generate report
    python generate_crash_report.py

    # Skip analysis, just regenerate HTML from existing logs
    python generate_crash_report.py --skip-analysis

    # Override paths
    python generate_crash_report.py --dump-dir D:\\dumps --output report.html

    # Open the report in the default browser after generating
    python generate_crash_report.py --open
"""
from __future__ import annotations

import argparse
import glob as glob_mod
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ── Auto-detected paths (overridable via CLI) ──────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUMP_DIR = REPO_ROOT / "crashdumps"
ANALYSIS_DIR = DUMP_DIR / ".analysis"
OUTPUT_PATH = DUMP_DIR / "crash-analysis-report.html"
MONKEY_LOG_DIR = REPO_ROOT / "src" / "monkey_logs"

# ── System info (detected at import time) ──────────────────────────────────
HARNESS = "Monkey Stress Tester"


def _detect_wt_version() -> str:
    """Try to read Windows Terminal version from the Appx package list."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage Microsoft.WindowsTerminal).Version"],
            capture_output=True, text=True, timeout=10,
        )
        version = result.stdout.strip()
        if version:
            return version
    except Exception:
        pass
    return "unknown"


def _detect_debugger_version(cdb_path: Path | None) -> str:
    """Return a short version string for the cdb we found."""
    if cdb_path is None:
        return "cdb not found"
    try:
        result = subprocess.run(
            [str(cdb_path), "-version"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines() + result.stderr.splitlines():
            if "cdb version" in line.lower() or "Microsoft" in line:
                return line.strip()
    except Exception:
        pass
    return f"cdb @ {cdb_path}"


WT_VERSION = _detect_wt_version()
OS_LABEL = platform.platform()


def _detect_terminal_source_revision() -> str:
    """Best-effort detection of the local microsoft/terminal checkout revision."""
    try:
        source_repo = REPO_ROOT.parent / "terminal"
        if not source_repo.is_dir():
            return "microsoft/terminal not found"
        result = subprocess.run(
            ["git", "-C", str(source_repo), "--no-pager", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        sha = result.stdout.strip()
        if sha:
            return f"microsoft/terminal @{sha}"
    except Exception:
        pass
    return "microsoft/terminal (revision unknown)"


TERMINAL_SOURCE_REV = _detect_terminal_source_revision()


# ── cdb discovery ──────────────────────────────────────────────────────────
def find_cdb() -> Path | None:
    """Search well-known locations for cdb.exe and return its path (or None)."""
    # 1. WinDbg Store app package (query via PowerShell — glob can't enumerate WindowsApps)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage Microsoft.WinDbg).InstallLocation"],
            capture_output=True, text=True, timeout=10,
        )
        install_loc = result.stdout.strip()
        if install_loc:
            cdb = Path(install_loc) / "amd64" / "cdb.exe"
            if cdb.is_file():
                return cdb
    except Exception:
        pass

    # 2. Fallback: glob for Store app (works when WindowsApps is readable)
    pattern = r"C:\Program Files\WindowsApps\Microsoft.WinDbg_*\amd64\cdb.exe"
    matches = sorted(glob_mod.glob(pattern))
    if matches:
        return Path(matches[-1])

    # 3. Windows SDK
    sdk_path = Path(
        r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"
    )
    if sdk_path.is_file():
        return sdk_path

    # 4. On PATH
    which = shutil.which("cdb")
    if which:
        return Path(which)

    return None


# ── cdb analysis runner ───────────────────────────────────────────────────
def run_cdb_analysis(
    cdb_path: Path,
    dump_dir: Path,
    analysis_dir: Path,
) -> None:
    """Run cdb on every .dmp in *dump_dir*, writing logs to *analysis_dir*."""
    analysis_dir.mkdir(parents=True, exist_ok=True)

    dumps = sorted(
        p for p in dump_dir.iterdir()
        if p.is_file() and p.name.endswith(".dmp")
    )
    if not dumps:
        print("No .dmp files found — nothing to analyse.")
        return

    for dump_path in dumps:
        name = dump_path.name
        if name.startswith("WindowsTerminal.exe"):
            # WER crash dump
            log_path = analysis_dir / f"{name}.quick.txt"
            cdb_commands = "!analyze -show; .ecxr; .exr -1; kvn 15; q"
        elif name.startswith("WindowsTerminal_"):
            # Watchdog hang dump
            log_path = analysis_dir / f"{name}.hang.txt"
            cdb_commands = "!analyze -hang; q"
        else:
            print(f"  skip  {name} (unrecognised naming pattern)")
            continue

        if log_path.is_file():
            print(f"  skip  {name} (analysis log already exists)")
            continue

        print(f"  analyse  {name} …", end="", flush=True)
        try:
            subprocess.run(
                [
                    str(cdb_path),
                    "-z", str(dump_path),
                    "-logo", str(log_path),
                    "-c", cdb_commands,
                ],
                capture_output=True, timeout=120,
            )
            print(" done")
        except subprocess.TimeoutExpired:
            print(" TIMEOUT")
        except Exception as exc:
            print(f" ERROR: {exc}")


# ── Constants ──────────────────────────────────────────────────────────────

EXCEPTION_LABELS = {
    "c0000005": "STATUS_ACCESS_VIOLATION",
    "c000041d": "STATUS_FATAL_USER_CALLBACK_EXCEPTION",
}

CRASH_FAMILY_INFO = {
    "pane-min-size": {
        "title": "Pane minimum-size / resize crash",
        "short": "Null dereference while TerminalApp computes pane minimum size during resize.",
        "detail": (
            "The failing instruction dereferences a null pane-content interface while "
            "`Pane::_GetMinSize` is executing in the resize path. The stack then walks "
            "through `Pane::_Resize`, `Pane::ResizePane`, and `TerminalPage::_HandleResizePane`, "
            "which makes this the strongest resize/layout crash family in the dump set."
        ),
        "risk": "Resize/layout state can become inconsistent or point at already-invalid pane content.",
    },
    "textbuffer-selectall": {
        "title": "TextBuffer::GetSize during SelectAll",
        "short": "Near-null read from `TextBuffer::GetSize` while processing Select All.",
        "detail": (
            "The stack shows `Terminal::SelectAll` flowing through `ControlCore::SelectAll`, "
            "`TermControl::SelectAll`, and `TerminalPage::_HandleSelectAll`. The fault address "
            "is a small offset (`0xD8`), which strongly suggests a null or stale `TextBuffer`-backed object."
        ),
        "risk": "Control-side state mutation races with selection actions.",
    },
    "cursor-mark-mode": {
        "title": "Cursor::SetIsOn during ToggleMarkMode",
        "short": "Near-null write while mark mode toggles the cursor state.",
        "detail": (
            "This family faults in `Cursor::SetIsOn`, reached via `Terminal::ToggleMarkMode`, "
            "`ControlCore::ToggleMarkMode`, and `TerminalPage::_HandleMarkMode`. The write target "
            "is at offset `0x112`, again pointing to a missing or stale cursor object rather than bad input text."
        ),
        "risk": "Cursor lifetime/state is not robust to rapid action dispatch.",
    },
    "command-history": {
        "title": "Command history / suggestions crash",
        "short": "Near-null read in `TextBuffer::_estimateOffsetOfLastCommittedRow` during command history handling.",
        "detail": (
            "The stack walks through `TextBuffer::Commands`, `ControlCore::CommandHistory`, "
            "and `TerminalPage::_HandleSuggestions` / `_doHandleSuggestions`, so this crash family "
            "appears tied to command-history or suggestions handling in the control layer."
        ),
        "risk": "Command-history state and buffer ownership drift out of sync under stress.",
    },
}

HANG_FAMILY_INFO = {
    "APPLICATION_HANG_BusyHang_Memory_cfffffff_ucrtbase.dll!free_base": {
        "title": "Allocator busy-hang in ucrtbase!free_base",
        "detail": (
            "These watchdog captures bucket as allocator-side busy hangs in `ucrtbase!free_base`, "
            "which is consistent with the process being stuck in heavy cleanup or memory churn rather than "
            "a clean idle wait."
        ),
    },
    "APPLICATION_HANG_cfffffff_win32u.dll!NtUserGetMessage": {
        "title": "UI message loop hang in NtUserGetMessage",
        "detail": (
            "These captures look like Windows Terminal was unresponsive while the UI thread sat in the "
            "message loop. That does not prove the message pump itself is the root cause, but it does confirm "
            "the process was observed in a frozen UI state rather than throwing an exception."
        ),
    },
    "APPLICATION_HANG_cfffffff_win32u.dll!NtUserTranslateMessage": {
        "title": "UI translation hang in NtUserTranslateMessage",
        "detail": (
            "This is another UI-thread hang bucket, slightly earlier in the message-processing path. "
            "It likely belongs to the same broad class of hangs as `NtUserGetMessage`, but Windows bucketed it separately."
        ),
    },
    "APPLICATION_HANG_BusyHang_cfffffff_TerminalApp.dll!Pane::LayoutSizeNode::LayoutSizeNode": {
        "title": "Pane layout construction busy-hang",
        "detail": (
            "This bucket points directly at pane layout construction inside `TerminalApp.dll`, which lines up "
            "with resize/layout pressure rather than text-only activity."
        ),
    },
    "APPLICATION_HANG_HungIn_ExceptionHandler_cfffffff_TerminalApp.dll!Pane::_GetMinSize": {
        "title": "Resize exception-handler hang in Pane::_GetMinSize",
        "detail": (
            "This hang family overlaps with the resize crash family. It suggests some resize failures stall in "
            "or around exception handling before the process finally terminates or is killed."
        ),
    },
}


SOURCE_RCA_AREAS = [
    {
        "title": "Pane-tree lifetime under resize / split / close churn",
        "crash_families": ["pane-min-size"],
        "hang_buckets": [
            "APPLICATION_HANG_BusyHang_cfffffff_TerminalApp.dll!Pane::LayoutSizeNode::LayoutSizeNode",
            "APPLICATION_HANG_HungIn_ExceptionHandler_cfffffff_TerminalApp.dll!Pane::_GetMinSize",
        ],
        "source_paths": [
            r"src\cascadia\TerminalApp\Pane.cpp :: Pane::_GetMinSize, Pane::_Resize, Pane::ResizePane, Pane::_CloseChild",
            r"src\cascadia\TerminalApp\Pane.LayoutSizeNode.cpp :: Pane::LayoutSizeNode::LayoutSizeNode",
            r"src\cascadia\TerminalApp\AppActionHandlers.cpp :: TerminalPage::_HandleResizePane",
        ],
        "hypothesis": (
            "Leaf-pane content can become stale while pane topology mutates under heavy resize/split/close action churn, "
            "leaving minimum-size queries to walk invalid content state."
        ),
        "fix_steps": [
            "Assert and harden pane invariants before dereferencing pane content in minimum-size and layout paths.",
            "Short-circuit resize processing if pane-content ownership is being transferred or pane close is in progress.",
            "Add targeted diagnostics around pane-tree mutation (split/close/reparent) to capture ownership transitions.",
        ],
    },
    {
        "title": "Control-core action forwarding while control lifetime is changing",
        "crash_families": ["textbuffer-selectall", "cursor-mark-mode", "command-history"],
        "hang_buckets": [],
        "source_paths": [
            r"src\cascadia\TerminalControl\TermControl.cpp :: SelectAll, ToggleMarkMode, CommandHistory",
            r"src\cascadia\TerminalControl\ControlCore.cpp :: SelectAll, ToggleMarkMode, CommandHistory",
            r"src\cascadia\TerminalCore\TerminalSelection.cpp :: Terminal::SelectAll, Terminal::ToggleMarkMode",
            r"src\buffer\out\textBuffer.cpp :: TextBuffer::GetSize, TextBuffer::_estimateOffsetOfLastCommittedRow",
        ],
        "hypothesis": (
            "Action handlers can continue forwarding into control/core objects while terminal or cursor/buffer state is "
            "tearing down, producing near-null reads/writes in TextBuffer and Cursor code."
        ),
        "fix_steps": [
            "Add explicit closing/initialized guards in TermControl action entry points before forwarding to ControlCore.",
            "Enforce core-side readiness checks for SelectAll, ToggleMarkMode, and CommandHistory paths.",
            "Add defensive validation around active buffer/cursor access to fail fast with telemetry instead of AV.",
        ],
    },
    {
        "title": "Mixed hang routes indicate secondary pressure, not a single hang root cause",
        "crash_families": [],
        "hang_buckets": [
            "APPLICATION_HANG_cfffffff_win32u.dll!NtUserGetMessage",
            "APPLICATION_HANG_cfffffff_win32u.dll!NtUserTranslateMessage",
            "APPLICATION_HANG_BusyHang_Memory_cfffffff_ucrtbase.dll!free_base",
        ],
        "source_paths": [
            r"Watchdog captures split across UI loop, allocator churn, and pane layout code paths.",
            r"Monkey summaries show crash-adjacent action bursts spanning layout, navigation, input, and mouse surfaces.",
        ],
        "hypothesis": (
            "UI-loop and allocator hangs are likely downstream symptoms of stress-induced state churn. "
            "The pane/layout and control-lifetime crash families are better candidates for the primary fix focus."
        ),
        "fix_steps": [
            "Prioritize fixing pane/layout and control-lifetime crash families first, then re-sample hang buckets.",
            "Add watchdog annotations to correlate hangs with the most recent action profile and crash events.",
        ],
    },
]


FIX_VALIDATION_PLAN = [
    "Phase 1 (hardening): add close/initialized guards plus pane-content invariant checks in hot action paths.",
    "Phase 2 (instrumentation): emit structured telemetry for pane ownership transitions and rejected actions.",
    "Phase 3 (regression): run 10-30 minute monkey sweeps across all-surfaces, buffer-chaos, and scroll-race profiles.",
    "Phase 4 (exit criteria): no c0000005/c000041d crashes in repeated runs and reduced pane/layout hang bucket frequency.",
]


def html_escape(value: object) -> str:
    return html.escape(str(value))


def format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def parse_stack_lines(text: str) -> list[str]:
    frames: list[str] = []
    in_stack = False
    for line in text.splitlines():
        if "# Child-SP" in line:
            in_stack = True
            continue
        if not in_stack:
            continue
        if line.startswith("quit:") or not line.strip():
            break
        if " : " not in line:
            continue
        left = line.lstrip()
        if not left or not re.match(r"^[0-9A-Fa-f]{2}\b", left):
            continue
        frames.append(line.rsplit(" : ", 1)[1].strip())
    return frames


def detect_crash_family(symbol: str, frames: list[str]) -> str:
    joined = " ".join([symbol] + frames)
    if "Pane::_GetMinSize" in joined or "consume_TerminalApp_IPaneContent" in joined:
        return "pane-min-size"
    if "TextBuffer::GetSize" in joined and "SelectAll" in joined:
        return "textbuffer-selectall"
    if "Cursor::SetIsOn" in joined or "ToggleMarkMode" in joined:
        return "cursor-mark-mode"
    if "TextBuffer::_estimateOffsetOfLastCommittedRow" in joined or "CommandHistory" in joined:
        return "command-history"
    return "unknown-crash"


def parse_crash_log(log_path: Path) -> dict[str, object]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    symbol_match = re.search(r"ExceptionAddress:\s*([0-9A-Fa-f`]+)\s+\(([^)]+)\)", text)
    code_match = re.search(r"ExceptionCode:\s*([0-9a-f]+)", text)
    flags_match = re.search(r"ExceptionFlags:\s*([0-9A-Fa-f]+)", text)
    access_match = re.search(
        r"Attempt to (read|write) (?:from|to) address ([0-9A-Fa-f`]+)", text
    )
    debug_match = re.search(r"Debug session time:\s*(.+)", text)
    uptime_match = re.search(r"Process Uptime:\s*(.+)", text)
    frames = parse_stack_lines(text)
    symbol = symbol_match.group(2) if symbol_match else "unknown"
    family = detect_crash_family(symbol, frames)
    return {
        "debug_time": debug_match.group(1).strip() if debug_match else "",
        "process_uptime": uptime_match.group(1).strip() if uptime_match else "",
        "exception_address": symbol_match.group(1) if symbol_match else "",
        "symbol": symbol,
        "exception_code": code_match.group(1).lower() if code_match else "",
        "exception_flags": flags_match.group(1) if flags_match else "",
        "access_kind": access_match.group(1) if access_match else "",
        "access_address": access_match.group(2) if access_match else "",
        "frames": frames[:12],
        "family": family,
    }


def parse_hang_log(log_path: Path) -> dict[str, object]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    debug_match = re.search(r"Debug session time:\s*(.+)", text)
    uptime_match = re.search(r"Process Uptime:\s*(.+)", text)
    return {
        "debug_time": debug_match.group(1).strip() if debug_match else "",
        "process_uptime": uptime_match.group(1).strip() if uptime_match else "",
        "process_name": re.search(r"PROCESS_NAME:\s+(.+)", text).group(1).strip(),
        "error_code": re.search(r"ERROR_CODE:\s+(.+)", text).group(1).strip(),
        "symbol_name": re.search(r"SYMBOL_NAME:\s+(.+)", text).group(1).strip(),
        "module_name": re.search(r"MODULE_NAME:\s+(.+)", text).group(1).strip(),
        "image_name": re.search(r"IMAGE_NAME:\s+(.+)", text).group(1).strip(),
        "failure_bucket": re.search(r"FAILURE_BUCKET_ID:\s+(.+)", text).group(1).strip(),
        "failure_hash": (
            re.search(r"FAILURE_ID_HASH:\s+\{([^}]+)\}", text).group(1).strip()
            if re.search(r"FAILURE_ID_HASH:\s+\{([^}]+)\}", text)
            else ""
        ),
    }


def dump_pid(name: str) -> str:
    if name.startswith("WindowsTerminal.exe"):
        match = re.search(r"\.(\d+)\.dmp", name)
    else:
        match = re.search(r"WindowsTerminal_(\d+)_", name)
    return match.group(1) if match else ""


def build_entries() -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for dump_path in sorted(DUMP_DIR.iterdir(), key=lambda p: p.stat().st_mtime):
        if not dump_path.is_file() or not dump_path.name.endswith(".dmp"):
            continue
        stat = dump_path.stat()
        if dump_path.name.startswith("WindowsTerminal.exe"):
            log_path = ANALYSIS_DIR / f"{dump_path.name}.quick.txt"
            if not log_path.is_file():
                print(f"  warning: no analysis log for {dump_path.name}, skipping")
                continue
            analysis = parse_crash_log(log_path)
            kind = "WER crash dump"
            duplicate = "(1)" in dump_path.name
            entry = {
                "name": dump_path.name,
                "path": str(dump_path),
                "kind": kind,
                "duplicate": duplicate,
                "pid": dump_pid(dump_path.name),
                "size_bytes": stat.st_size,
                "size_label": format_size(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime),
                "analysis": analysis,
            }
        elif dump_path.name.startswith("WindowsTerminal_"):
            log_path = ANALYSIS_DIR / f"{dump_path.name}.hang.txt"
            if not log_path.is_file():
                print(f"  warning: no analysis log for {dump_path.name}, skipping")
                continue
            analysis = parse_hang_log(log_path)
            entry = {
                "name": dump_path.name,
                "path": str(dump_path),
                "kind": "Watchdog hang dump",
                "duplicate": False,
                "pid": dump_pid(dump_path.name),
                "size_bytes": stat.st_size,
                "size_label": format_size(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime),
                "analysis": analysis,
            }
        else:
            continue
        entries.append(entry)
    return entries


def shorten_symbol(sym: str, max_len: int = 80) -> str:
    """Shorten long WinRT symbols for display while keeping the meaningful part."""
    # Strip long template prefixes, keep the tail
    if len(sym) <= max_len:
        return sym
    # Try to find the last meaningful function name
    parts = sym.split("::")
    # Walk backwards to build a short version
    short = parts[-1]
    for p in reversed(parts[:-1]):
        candidate = p + "::" + short
        if len(candidate) > max_len:
            return "…::" + short
        short = candidate
    return short


def render_callchain(frames: list[str]) -> str:
    if not frames:
        return '<p class="muted">No stack frames were parsed.</p>'
    items = []
    for idx, frame in enumerate(frames[:10]):
        css_class = "frame-fault" if idx == 0 else "frame-context" if idx < 3 else "frame-tail"
        num = f'<span class="frame-num">{idx:02d}</span>'
        items.append(f'<div class="stack-frame {css_class}">{num}<code>{html_escape(shorten_symbol(frame))}</code></div>')
    return '<div class="stack-trace">' + "".join(items) + "</div>"


def render_stat_cards(entries, crash_entries, hang_entries, unique_crash_families, unique_hang_buckets) -> str:
    total_size = sum(e["size_bytes"] for e in entries)
    return f"""
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-value">{len(entries)}</div>
        <div class="stat-label">Total Dumps</div>
      </div>
      <div class="stat-card stat-crash">
        <div class="stat-value">{len(crash_entries)}</div>
        <div class="stat-label">WER Crashes</div>
      </div>
      <div class="stat-card stat-hang">
        <div class="stat-value">{len(hang_entries)}</div>
        <div class="stat-label">Hang Captures</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{len(unique_crash_families)}</div>
        <div class="stat-label">Crash Families</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{len(unique_hang_buckets)}</div>
        <div class="stat-label">Hang Buckets</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{format_size(total_size)}</div>
        <div class="stat-label">Total Dump Size</div>
      </div>
    </div>"""


def render_inventory(entries: list[dict[str, object]]) -> str:
    rows = []
    for entry in entries:
        analysis = entry["analysis"]
        if entry["kind"] == "WER crash dump":
            badge = '<span class="pill pill-crash">CRASH</span>'
            summary = shorten_symbol(analysis["symbol"], 60)
        else:
            badge = '<span class="pill pill-hang">HANG</span>'
            summary = shorten_symbol(analysis["symbol_name"], 60)
        dup_class = " row-dim" if entry.get("duplicate") else ""
        rows.append(
            f'<tr class="{dup_class}">'
            f'<td class="col-time">{html_escape(entry["mtime"].strftime("%H:%M:%S"))}</td>'
            f"<td>{badge}</td>"
            f'<td class="col-pid">{html_escape(entry["pid"])}</td>'
            f'<td class="col-size">{html_escape(entry["size_label"])}</td>'
            f'<td class="col-symbol"><code>{html_escape(summary)}</code></td>'
            f'<td class="col-file"><code>{html_escape(entry["name"])}</code></td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="inventory-table">'
        '<thead><tr><th>Time</th><th>Type</th><th>PID</th><th>Size</th>'
        "<th>Primary Symbol</th><th>Dump File</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_crash_families(crash_entries: list[dict[str, object]]) -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in crash_entries:
        grouped[entry["analysis"]["family"]].append(entry)

    sections = []
    for idx, (family_key, entries) in enumerate(grouped.items(), 1):
        info = CRASH_FAMILY_INFO.get(
            family_key,
            {"title": family_key, "short": "Unclassified.", "detail": "", "risk": "Unknown"},
        )
        primary = entries[0]["analysis"]
        dumps_html = "".join(
            f'<span class="file-pill">{html_escape(e["name"])}</span>' for e in entries
        )
        codes = Counter(e["analysis"]["exception_code"] for e in entries)
        code_pills = "".join(
            f'<span class="pill pill-code">{html_escape(code)}</span> '
            f'<span class="muted">×{count} {html_escape(EXCEPTION_LABELS.get(code, ""))}</span> '
            for code, count in sorted(codes.items())
        )
        access = (primary["access_kind"] + " @ " + primary["access_address"]).strip()
        if not primary["access_kind"]:
            access = "n/a"

        sections.append(f"""
        <div class="family-card">
          <div class="family-header crash-header">
            <span class="family-num">#{idx}</span>
            <h3>{html_escape(info['title'])}</h3>
            <span class="pill pill-crash">CRASH</span>
          </div>
          <div class="family-body">
            <p class="family-short">{html_escape(info['short'])}</p>
            <div class="detail-grid">
              <div class="detail-item"><span class="detail-label">Exception</span>{code_pills}</div>
              <div class="detail-item"><span class="detail-label">Access</span><code>{html_escape(access)}</code></div>
              <div class="detail-item"><span class="detail-label">Uptime</span>{html_escape(primary['process_uptime'])}</div>
              <div class="detail-item"><span class="detail-label">Dumps</span><div class="file-pills">{dumps_html}</div></div>
            </div>
            <p class="family-detail">{html_escape(info['detail'])}</p>
            <div class="risk-box"><strong>⚠ Risk:</strong> {html_escape(info['risk'])}</div>
            <details class="stack-details"><summary>Stack trace</summary>{render_callchain(primary["frames"])}</details>
          </div>
        </div>""")
    return "".join(sections)


def render_hang_families(hang_entries: list[dict[str, object]]) -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in hang_entries:
        grouped[entry["analysis"]["failure_bucket"]].append(entry)

    sections = []
    for idx, (bucket, entries) in enumerate(grouped.items(), 1):
        info = HANG_FAMILY_INFO.get(
            bucket, {"title": bucket, "detail": "No additional info."},
        )
        primary = entries[0]["analysis"]
        dumps_html = "".join(
            f'<span class="file-pill">{html_escape(e["name"])}</span>' for e in entries
        )
        sections.append(f"""
        <div class="family-card">
          <div class="family-header hang-header">
            <span class="family-num">#{idx}</span>
            <h3>{html_escape(info['title'])}</h3>
            <span class="pill pill-hang">HANG</span>
          </div>
          <div class="family-body">
            <div class="detail-grid">
              <div class="detail-item"><span class="detail-label">Bucket</span><code class="bucket-code">{html_escape(shorten_symbol(bucket, 70))}</code></div>
              <div class="detail-item"><span class="detail-label">Symbol</span><code>{html_escape(primary['symbol_name'])}</code></div>
              <div class="detail-item"><span class="detail-label">Module</span><code>{html_escape(primary['module_name'])}</code> / <code>{html_escape(primary['image_name'])}</code></div>
              <div class="detail-item"><span class="detail-label">Uptime</span>{html_escape(primary['process_uptime'])}</div>
              <div class="detail-item"><span class="detail-label">Dumps</span><div class="file-pills">{dumps_html}</div></div>
            </div>
            <p class="family-detail">{html_escape(info['detail'])}</p>
          </div>
        </div>""")
    return "".join(sections)


def render_per_dump_details(entries: list[dict[str, object]]) -> str:
    parts = []
    for entry in entries:
        if entry["kind"] == "WER crash dump":
            a = entry["analysis"]
            badge = '<span class="pill pill-crash">CRASH</span>'
            access = (a["access_kind"] + " @ " + a["access_address"]).strip() or "n/a"
            dup_note = ' <span class="pill pill-dim">DUPLICATE</span>' if entry["duplicate"] else ""
            inner = f"""
              <div class="detail-grid">
                <div class="detail-item"><span class="detail-label">Exception</span><code>{html_escape(a['exception_code'])}</code> <span class="muted">{html_escape(EXCEPTION_LABELS.get(a['exception_code'], ''))}</span></div>
                <div class="detail-item"><span class="detail-label">Flags</span><code>{html_escape(a['exception_flags'])}</code></div>
                <div class="detail-item"><span class="detail-label">Symbol</span><code>{html_escape(shorten_symbol(a['symbol'], 70))}</code></div>
                <div class="detail-item"><span class="detail-label">Address</span><code>{html_escape(a['exception_address'])}</code></div>
                <div class="detail-item"><span class="detail-label">Access</span><code>{html_escape(access)}</code></div>
                <div class="detail-item"><span class="detail-label">Uptime</span>{html_escape(a['process_uptime'])}</div>
              </div>
              <details class="stack-details"><summary>Stack trace</summary>{render_callchain(a["frames"])}</details>"""
        else:
            a = entry["analysis"]
            badge = '<span class="pill pill-hang">HANG</span>'
            dup_note = ""
            inner = f"""
              <div class="detail-grid">
                <div class="detail-item"><span class="detail-label">Bucket</span><code class="bucket-code">{html_escape(shorten_symbol(a['failure_bucket'], 70))}</code></div>
                <div class="detail-item"><span class="detail-label">Symbol</span><code>{html_escape(a['symbol_name'])}</code></div>
                <div class="detail-item"><span class="detail-label">Module</span><code>{html_escape(a['module_name'])}</code></div>
                <div class="detail-item"><span class="detail-label">Error</span><code>{html_escape(a['error_code'])}</code></div>
                <div class="detail-item"><span class="detail-label">Uptime</span>{html_escape(a['process_uptime'])}</div>
              </div>"""

        parts.append(f"""
        <details class="dump-details">
          <summary class="dump-header">
            {badge}{dup_note}
            <code class="dump-name">{html_escape(entry['name'])}</code>
            <span class="dump-meta">PID {html_escape(entry['pid'])} · {html_escape(entry['size_label'])} · {html_escape(entry['mtime'].strftime('%H:%M:%S'))}</span>
          </summary>
          <div class="dump-body">{inner}</div>
        </details>""")
    return "".join(parts)


def _collect_monkey_signal(max_files: int = 40) -> dict[str, object]:
    summary_files = sorted(
        MONKEY_LOG_DIR.glob("summary_*.json"),
        key=lambda p: p.stat().st_mtime,
    )[-max_files:]
    tag_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    crash_exit_codes: Counter[str] = Counter()
    crash_actions: Counter[str] = Counter()
    crash_events = 0
    hang_events = 0
    total_actions = 0

    for path in summary_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        total_actions += int(payload.get("total_actions", 0) or 0)
        tag_counts.update((payload.get("tag_counts") or {}))
        action_counts.update((payload.get("action_counts") or {}))

        for event in payload.get("crash_events") or []:
            if not isinstance(event, dict):
                continue
            crash_events += 1
            code = event.get("exit_code")
            if code is not None:
                crash_exit_codes[str(code)] += 1
            action = event.get("last_action")
            if action:
                crash_actions[str(action)] += 1

        for event in payload.get("hang_events") or []:
            if isinstance(event, dict):
                hang_events += 1

    return {
        "sampled_runs": len(summary_files),
        "total_actions": total_actions,
        "crash_events": crash_events,
        "hang_events": hang_events,
        "top_tags": tag_counts.most_common(6),
        "top_actions": action_counts.most_common(6),
        "crash_exit_codes": crash_exit_codes.most_common(4),
        "crash_actions": crash_actions.most_common(6),
    }


def _format_exit_code(value: str) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{numeric} (0x{(numeric & 0xFFFFFFFF):08X})"


def _render_count_list(items: list[tuple[str, int]], empty_label: str = "none observed") -> str:
    if not items:
        return f'<span class="muted">{html_escape(empty_label)}</span>'
    return ", ".join(
        f"<code>{html_escape(name)}</code> ×{count}" for name, count in items
    )


def render_source_rca(
    crash_family_counts: Counter[str],
    hang_bucket_counts: Counter[str],
    monkey_signal: dict[str, object],
) -> str:
    cards: list[str] = []
    for idx, area in enumerate(SOURCE_RCA_AREAS, 1):
        crash_refs = [
            (family, crash_family_counts.get(family, 0))
            for family in area["crash_families"]
            if crash_family_counts.get(family, 0) > 0
        ]
        hang_refs = [
            (bucket, hang_bucket_counts.get(bucket, 0))
            for bucket in area["hang_buckets"]
            if hang_bucket_counts.get(bucket, 0) > 0
        ]
        source_items = "".join(
            f"<li><code>{html_escape(path)}</code></li>" for path in area["source_paths"]
        )
        step_items = "".join(
            f"<li>{html_escape(step)}</li>" for step in area["fix_steps"]
        )
        cards.append(f"""
        <div class="finding-card rca-card">
          <h3><span class="finding-num">{idx}</span> {html_escape(area['title'])}</h3>
          <p><strong>Hypothesis:</strong> {html_escape(area['hypothesis'])}</p>
          <p><strong>Crash families:</strong> {_render_count_list(crash_refs)}</p>
          <p><strong>Hang buckets:</strong> {_render_count_list(hang_refs)}</p>
          <div class="two-col">
            <div>
              <h4>Source paths inspected</h4>
              <ul class="compact-list">{source_items}</ul>
            </div>
            <div>
              <h4>Immediate hardening actions</h4>
              <ul class="compact-list">{step_items}</ul>
            </div>
          </div>
        </div>""")

    top_tags = _render_count_list(monkey_signal["top_tags"], "no tag data")
    top_crash_actions = _render_count_list(monkey_signal["crash_actions"], "no crash-action data")
    exit_codes = ", ".join(
        f"<code>{html_escape(_format_exit_code(code))}</code> ×{count}"
        for code, count in monkey_signal["crash_exit_codes"]
    ) or '<span class="muted">no crash exit-code data</span>'

    cards.append(f"""
    <div class="finding-card signal-card">
      <h3><span class="finding-num">M</span> Monkey reproduction signal (supporting evidence)</h3>
      <p>
        Sampled <strong>{int(monkey_signal['sampled_runs'])}</strong> stress-run summaries with
        <strong>{int(monkey_signal['total_actions'])}</strong> total actions,
        <strong>{int(monkey_signal['crash_events'])}</strong> crash events, and
        <strong>{int(monkey_signal['hang_events'])}</strong> hang events.
      </p>
      <p><strong>Most common tags:</strong> {top_tags}</p>
      <p><strong>Crash-adjacent actions:</strong> {top_crash_actions}</p>
      <p><strong>Crash exit codes:</strong> {exit_codes}</p>
    </div>""")

    return "".join(cards)


def render_issue_draft(
    crash_entries: list[dict[str, object]],
    hang_entries: list[dict[str, object]],
    crash_family_counts: Counter[str],
    hang_bucket_counts: Counter[str],
) -> str:
    crash_lines = "\n".join(
        f"- {family}: {count} dump(s)"
        for family, count in crash_family_counts.most_common()
    )
    hang_lines = "\n".join(
        f"- {bucket}: {count} dump(s)"
        for bucket, count in hang_bucket_counts.most_common()
    )
    issue_text = f"""Title
Crash under pane/layout and control action churn (AV c0000005 with c000041d follow-ups)

Summary
Monkey stress testing produced {len(crash_entries)} WER crash dumps and {len(hang_entries)} watchdog hang dumps.
Crashes cluster into {len(crash_family_counts)} families, dominated by pane/layout and control action paths.

Observed crash families
{crash_lines or "- none"}

Observed hang buckets
{hang_lines or "- none"}

Primary stack paths
- TerminalApp!Pane::_GetMinSize / Pane::LayoutSizeNode::LayoutSizeNode during resize/layout actions
- TerminalControl + TerminalCore selection/history paths:
  SelectAll -> TextBuffer::GetSize
  ToggleMarkMode -> Cursor::SetIsOn
  CommandHistory -> TextBuffer::_estimateOffsetOfLastCommittedRow

Repro notes
- Triggered by high-rate action churn (resize/split/close/navigation/mouse/input) from monkey stress profiles.
- Follow-up crash dumps commonly record c000041d after a primary c0000005 AV.

Candidate fix direction
1) Harden pane-content lifetime invariants in resize/layout flows.
2) Add closing/initialized guards in control action entry points before forwarding to core.
3) Add defensive checks around active buffer/cursor state and emit telemetry on rejected actions.

Environment
- Source: {TERMINAL_SOURCE_REV}
- Harness: {HARNESS}
"""
    return f"""
    <div class="finding-card issue-draft-card">
      <h3><span class="finding-num">I</span> Suggested GitHub issue draft</h3>
      <p class="muted">Copy/paste this directly into a new issue.</p>
      <pre class="issue-draft">{html_escape(issue_text)}</pre>
    </div>"""


def render_fix_plan() -> str:
    phase_items = "".join(f"<li>{html_escape(step)}</li>" for step in FIX_VALIDATION_PLAN)
    scoped_steps = []
    for area in SOURCE_RCA_AREAS:
        scoped_steps.extend(area["fix_steps"])
    scoped_items = "".join(f"<li>{html_escape(step)}</li>" for step in scoped_steps)
    return f"""
    <div class="finding-card fix-plan-card">
      <h3><span class="finding-num">F</span> Fix + validation plan</h3>
      <div class="two-col">
        <div>
          <h4>Implementation track</h4>
          <ul class="compact-list">{scoped_items}</ul>
        </div>
        <div>
          <h4>Validation track</h4>
          <ul class="compact-list">{phase_items}</ul>
        </div>
      </div>
    </div>"""


def main() -> None:
    global DUMP_DIR, ANALYSIS_DIR, OUTPUT_PATH

    parser = argparse.ArgumentParser(
        description="Analyse Windows Terminal crash dumps and generate an HTML report.",
    )
    parser.add_argument(
        "--dump-dir", type=Path, default=None,
        help="Directory containing .dmp files (default: <repo>/crashdumps)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output HTML report path (default: <dump-dir>/crash-analysis-report.html)",
    )
    parser.add_argument(
        "--cdb", type=Path, default=None,
        help="Path to cdb.exe (default: auto-detect)",
    )
    parser.add_argument(
        "--skip-analysis", action="store_true",
        help="Skip cdb analysis; just generate HTML from existing logs",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the report in the default browser after generating",
    )
    args = parser.parse_args()

    # Apply overrides to module-level paths
    if args.dump_dir is not None:
        DUMP_DIR = args.dump_dir.resolve()
    if args.output is not None:
        OUTPUT_PATH = args.output.resolve()
    else:
        OUTPUT_PATH = DUMP_DIR / "crash-analysis-report.html"
    ANALYSIS_DIR = DUMP_DIR / ".analysis"

    if not DUMP_DIR.is_dir():
        print(f"Error: dump directory does not exist: {DUMP_DIR}", file=sys.stderr)
        sys.exit(1)

    # ── cdb analysis ───────────────────────────────────────────────────
    cdb_path: Path | None = None
    if not args.skip_analysis:
        cdb_path = args.cdb or find_cdb()
        if cdb_path is None:
            print(
                "Error: cdb.exe not found. Install WinDbg or the Windows SDK, "
                "or pass --cdb <path> or --skip-analysis.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Using cdb: {cdb_path}")
        run_cdb_analysis(cdb_path, DUMP_DIR, ANALYSIS_DIR)

    DEBUGGER_VERSION = _detect_debugger_version(cdb_path)

    # ── Build entries and generate HTML ────────────────────────────────
    entries = build_entries()
    crash_entries = [e for e in entries if e["kind"] == "WER crash dump"]
    hang_entries = [e for e in entries if e["kind"] == "Watchdog hang dump"]

    unique_crash_families = Counter(e["analysis"]["family"] for e in crash_entries)
    unique_hang_buckets = Counter(e["analysis"]["failure_bucket"] for e in hang_entries)
    duplicate_crash_count = sum(1 for e in crash_entries if e["duplicate"])
    monkey_signal = _collect_monkey_signal()

    stat_cards= render_stat_cards(entries, crash_entries, hang_entries, unique_crash_families, unique_hang_buckets)
    source_rca_cards = render_source_rca(unique_crash_families, unique_hang_buckets, monkey_signal)
    issue_draft_card = render_issue_draft(crash_entries, hang_entries, unique_crash_families, unique_hang_buckets)
    fix_plan_card = render_fix_plan()

    report = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Windows Terminal Crash Analysis Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-card-hover: #1c2129;
    --bg-surface: #21262d;
    --border: #30363d;
    --border-subtle: #21262d;
    --text: #e6edf3;
    --text-secondary: #8b949e;
    --text-muted: #6e7681;
    --accent: #58a6ff;
    --accent-dim: #1f6feb;
    --crash-red: #f85149;
    --crash-bg: rgba(248, 81, 73, 0.1);
    --crash-border: rgba(248, 81, 73, 0.4);
    --hang-amber: #d29922;
    --hang-bg: rgba(210, 153, 34, 0.1);
    --hang-border: rgba(210, 153, 34, 0.4);
    --green: #3fb950;
    --purple: #bc8cff;
    --code-bg: #0d1117;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', 'SF Mono', monospace;
    --radius: 8px;
    --radius-sm: 4px;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
  }}

  .container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 40px 24px;
  }}

  /* ── Header ────────────────────────────────────────── */
  .report-header {{
    text-align: center;
    padding: 60px 20px 40px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 40px;
    background: linear-gradient(180deg, rgba(88,166,255,0.06) 0%, transparent 100%);
  }}
  .report-header h1 {{
    font-size: 2rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin-bottom: 8px;
    background: linear-gradient(135deg, var(--text), var(--accent));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  .report-header .subtitle {{
    font-size: 0.85rem;
    color: var(--text-secondary);
    font-family: var(--font-mono);
  }}
  .meta-pills {{
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 16px;
  }}
  .meta-pill {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 4px 14px;
    font-size: 0.78rem;
    color: var(--text-secondary);
    font-family: var(--font-mono);
  }}
  .meta-pill .label {{ color: var(--text-muted); }}

  /* ── Section headers ───────────────────────────────── */
  .section-title {{
    font-size: 1.25rem;
    font-weight: 700;
    margin: 48px 0 20px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .section-title .icon {{ font-size: 1.3em; }}

  /* ── Stat cards ────────────────────────────────────── */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin: 24px 0;
  }}
  .stat-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    text-align: center;
    transition: border-color 0.15s;
  }}
  .stat-card:hover {{ border-color: var(--accent-dim); }}
  .stat-card .stat-value {{
    font-size: 1.8rem;
    font-weight: 700;
    font-family: var(--font-mono);
    color: var(--text);
  }}
  .stat-card .stat-label {{
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .stat-crash .stat-value {{ color: var(--crash-red); }}
  .stat-hang .stat-value {{ color: var(--hang-amber); }}

  /* ── Summary box ───────────────────────────────────── */
  .summary-box {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius);
    padding: 24px;
    margin: 24px 0;
  }}
  .summary-box h3 {{
    color: var(--accent);
    font-size: 1rem;
    margin-bottom: 12px;
  }}
  .summary-box p {{
    color: var(--text-secondary);
    margin-top: 8px;
  }}
  .summary-box strong {{ color: var(--text); }}

  /* ── Pills / badges ────────────────────────────────── */
  .pill {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.7rem;
    font-weight: 600;
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .pill-crash {{
    background: var(--crash-bg);
    color: var(--crash-red);
    border: 1px solid var(--crash-border);
  }}
  .pill-hang {{
    background: var(--hang-bg);
    color: var(--hang-amber);
    border: 1px solid var(--hang-border);
  }}
  .pill-code {{
    background: rgba(188, 140, 255, 0.1);
    color: var(--purple);
    border: 1px solid rgba(188, 140, 255, 0.3);
  }}
  .pill-dim {{
    background: var(--bg-surface);
    color: var(--text-muted);
    border: 1px solid var(--border);
  }}
  .file-pill {{
    display: inline-block;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 1px 8px;
    font-size: 0.75rem;
    font-family: var(--font-mono);
    color: var(--text-secondary);
    margin: 2px 4px 2px 0;
  }}
  .file-pills {{ display: flex; flex-wrap: wrap; gap: 4px; }}

  /* ── Inventory table ───────────────────────────────── */
  .table-wrap {{
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin: 16px 0;
  }}
  .inventory-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
    font-family: var(--font-mono);
  }}
  .inventory-table thead th {{
    background: var(--bg-surface);
    color: var(--text-secondary);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 0.7rem;
    letter-spacing: 0.06em;
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
  }}
  .inventory-table tbody tr {{
    border-bottom: 1px solid var(--border-subtle);
    transition: background 0.1s;
  }}
  .inventory-table tbody tr:hover {{ background: var(--bg-card-hover); }}
  .inventory-table td {{
    padding: 8px 12px;
    vertical-align: middle;
  }}
  .inventory-table code {{
    font-size: 0.78rem;
    color: var(--accent);
  }}
  .row-dim {{ opacity: 0.5; }}
  .col-time {{ white-space: nowrap; }}
  .col-pid {{ white-space: nowrap; color: var(--text-muted); }}
  .col-size {{ white-space: nowrap; color: var(--text-muted); }}

  /* ── Family cards ──────────────────────────────────── */
  .family-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin: 16px 0;
    overflow: hidden;
  }}
  .family-header {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
  }}
  .crash-header {{ background: linear-gradient(90deg, var(--crash-bg), transparent); }}
  .hang-header {{ background: linear-gradient(90deg, var(--hang-bg), transparent); }}
  .family-num {{
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--text-muted);
    background: var(--bg-surface);
    border-radius: 50%;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .family-header h3 {{
    flex: 1;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
  }}
  .family-body {{
    padding: 20px;
  }}
  .family-short {{
    color: var(--text-secondary);
    font-size: 0.92rem;
    margin-bottom: 16px;
  }}
  .family-detail {{
    color: var(--text-muted);
    font-size: 0.85rem;
    margin-top: 12px;
    line-height: 1.6;
  }}

  /* ── Detail grid ───────────────────────────────────── */
  .detail-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 10px;
    margin: 12px 0;
  }}
  .detail-item {{
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 8px 12px;
    background: var(--bg-surface);
    border-radius: var(--radius-sm);
    font-size: 0.82rem;
  }}
  .detail-label {{
    font-size: 0.68rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }}
  .detail-item code {{
    font-family: var(--font-mono);
    color: var(--accent);
    font-size: 0.8rem;
    word-break: break-all;
  }}
  .bucket-code {{ color: var(--hang-amber) !important; }}

  /* ── Risk box ──────────────────────────────────────── */
  .risk-box {{
    margin-top: 12px;
    padding: 10px 16px;
    background: var(--crash-bg);
    border: 1px solid var(--crash-border);
    border-radius: var(--radius-sm);
    font-size: 0.85rem;
    color: var(--crash-red);
  }}
  .risk-box strong {{ color: var(--crash-red); }}

  /* ── Stack traces ──────────────────────────────────── */
  .stack-details {{
    margin-top: 16px;
  }}
  .stack-details summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.82rem;
    font-weight: 500;
    padding: 6px 0;
    user-select: none;
  }}
  .stack-details summary:hover {{ text-decoration: underline; }}
  .stack-trace {{
    margin-top: 8px;
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
    overflow-x: auto;
  }}
  .stack-frame {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 3px 0;
    font-size: 0.78rem;
    font-family: var(--font-mono);
    border-left: 2px solid transparent;
    padding-left: 8px;
  }}
  .frame-num {{
    color: var(--text-muted);
    min-width: 20px;
    text-align: right;
    user-select: none;
  }}
  .frame-fault {{
    border-left-color: var(--crash-red);
    background: var(--crash-bg);
    border-radius: var(--radius-sm);
    padding: 4px 8px;
  }}
  .frame-fault code {{ color: var(--crash-red); font-weight: 500; }}
  .frame-context code {{ color: var(--text); }}
  .frame-tail code {{ color: var(--text-muted); }}

  /* ── Dump cards (per-dump) — now collapsible ────────── */
  .dump-details {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin: 8px 0;
    overflow: hidden;
  }}
  .dump-details[open] {{ border-color: var(--accent-dim); }}
  .dump-details .dump-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    background: var(--bg-surface);
    cursor: pointer;
    flex-wrap: wrap;
    list-style: none;
    user-select: none;
  }}
  .dump-details .dump-header::-webkit-details-marker {{ display: none; }}
  .dump-details .dump-header::before {{
    content: '▶';
    font-size: 0.65rem;
    color: var(--text-muted);
    transition: transform 0.15s;
  }}
  .dump-details[open] .dump-header::before {{ transform: rotate(90deg); }}
  .dump-details .dump-header:hover {{ background: var(--bg-card-hover); }}
  .dump-name {{
    font-family: var(--font-mono);
    font-size: 0.82rem;
    color: var(--text);
    font-weight: 500;
  }}
  .dump-meta {{
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-left: auto;
    font-family: var(--font-mono);
  }}
  .dump-body {{ padding: 16px; }}

  /* ── Findings cards ────────────────────────────────── */
  .finding-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent-dim);
    border-radius: var(--radius);
    padding: 20px 24px;
    margin: 12px 0;
    position: relative;
  }}
  .finding-card h3 {{
    font-size: 0.95rem;
    color: var(--accent);
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .finding-num {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--accent-dim);
    color: #fff;
    font-size: 0.72rem;
    font-weight: 700;
    font-family: var(--font-mono);
    flex-shrink: 0;
  }}
  .finding-card p {{
    color: var(--text-secondary);
    font-size: 0.88rem;
  }}
  .finding-card code {{
    font-family: var(--font-mono);
    background: var(--bg-surface);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.82rem;
    color: var(--accent);
  }}
  .finding-card h4 {{
    margin: 8px 0;
    font-size: 0.82rem;
    color: var(--text);
  }}
  .two-col {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 14px;
    margin-top: 10px;
  }}
  .compact-list {{
    margin: 0;
    padding-left: 18px;
    color: var(--text-secondary);
    font-size: 0.83rem;
  }}
  .compact-list li {{
    margin: 5px 0;
  }}
  .issue-draft {{
    margin-top: 10px;
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 0.78rem;
    line-height: 1.6;
    white-space: pre-wrap;
  }}

  /* ── Section dividers ──────────────────────────────── */
  .section-divider {{
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border), transparent);
    margin: 48px 0 8px;
  }}

  /* ── Footer ────────────────────────────────────────── */
  .report-footer {{
    margin-top: 60px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    text-align: center;
    font-size: 0.78rem;
    color: var(--text-muted);
    font-family: var(--font-mono);
  }}

  .muted {{ color: var(--text-muted); }}
</style>
</head>
<body>

<div class="report-header">
  <h1>Windows Terminal Crash Analysis Report</h1>
  <p class="subtitle">Automated dump triage from monkey stress testing</p>
  <div class="meta-pills">
    <span class="meta-pill"><span class="label">Generated</span> {html_escape(datetime.now().strftime('%Y-%m-%d %H:%M'))}</span>
    <span class="meta-pill"><span class="label">WT</span> {html_escape(WT_VERSION)}</span>
    <span class="meta-pill"><span class="label">OS</span> {html_escape(OS_LABEL)}</span>
    <span class="meta-pill"><span class="label">Debugger</span> {html_escape(DEBUGGER_VERSION)}</span>
    <span class="meta-pill"><span class="label">Harness</span> {html_escape(HARNESS)}</span>
  </div>
</div>

<div class="container">

{stat_cards}

<div class="summary-box">
  <h3>Executive Summary</h3>
  <p>
    Analyzed <strong>{len(entries)}</strong> dump files with <code>cdb.exe</code>:
    <strong>{len(crash_entries)}</strong> WER crash dumps and
    <strong>{len(hang_entries)}</strong> watchdog-captured hang dumps.
  </p>
  <p>
    The WER side collapses into <strong>{len(unique_crash_families)}</strong> distinct crash families —
    all user-mode access violations in Windows Terminal code, dominated by null or near-null pointer
    dereferences during rapid keyboard-driven action dispatch.
    The hang side collapses into <strong>{len(unique_hang_buckets)}</strong> distinct hang buckets.
  </p>
  <p>
    Of the WER crash dumps, <strong>{duplicate_crash_count}</strong> are duplicate follow-up captures
    (the <code>(1)</code> files) showing <code>c000041d</code> (fatal user callback exception)
    after the primary <code>c0000005</code> access violation.
  </p>
  <p>
    Source alignment was performed against <code>{html_escape(TERMINAL_SOURCE_REV)}</code> and mapped to
    concrete TerminalApp/TerminalControl/TerminalCore code paths for an actionable fix plan.
  </p>
</div>

<h2 class="section-title"><span class="icon">📋</span> Dump Inventory</h2>
{render_inventory(entries)}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">💥</span> Crash Families</h2>
{render_crash_families(crash_entries)}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">⏳</span> Hang Families</h2>
{render_hang_families(hang_entries)}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">🔍</span> Per-Dump Details</h2>
<p class="muted" style="margin-bottom:12px; font-size:0.85rem;">Click any dump to expand its details.</p>
{render_per_dump_details(entries)}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">📊</span> Cross-Cutting Findings</h2>

<div class="finding-card">
  <h3><span class="finding-num">1</span> The crashes are real WT bugs, not focus mishaps</h3>
  <p>
    Every WER dump faults inside Windows Terminal code paths.
    Primary failures are access violations in <code>TerminalApp.dll</code> or
    <code>Microsoft.Terminal.Control.dll</code>, with stacks flowing through
    <code>TerminalPage</code> and <code>ShortcutActionDispatch::DoAction</code>.
  </p>
</div>

<div class="finding-card">
  <h3><span class="finding-num">2</span> Resize / layout is the strongest repeated family</h3>
  <p>
    The pane resize crash in <code>Pane::_GetMinSize</code> has a matching watchdog hang bucket
    in the same function. A second hang bucket targets
    <code>Pane::LayoutSizeNode::LayoutSizeNode</code>, reinforcing that resize/layout
    pressure is the most repeatable failure area.
  </p>
</div>

<div class="finding-card">
  <h3><span class="finding-num">3</span> Control-side state mutation is the other main family</h3>
  <p>
    Three independent crash families land in control-layer objects:
    <code>TextBuffer::GetSize</code> during Select All,
    <code>Cursor::SetIsOn</code> during mark-mode toggling, and
    <code>TextBuffer::_estimateOffsetOfLastCommittedRow</code> during command history.
    All three look like stale or null object access under rapid action churn.
  </p>
</div>

<div class="finding-card">
  <h3><span class="finding-num">4</span> The hang dumps are mixed — not one single hang signature</h3>
  <p>
    Some hang buckets point at the UI message loop (<code>NtUserGetMessage</code>,
    <code>NtUserTranslateMessage</code>), some at allocator churn (<code>ucrtbase!free_base</code>),
    and some directly at pane/layout code. That split suggests multiple routes to
    non-responsiveness rather than one universal hang root cause.
  </p>
</div>

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">🧠</span> Source-Level Root Cause Analysis</h2>
{source_rca_cards}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">📝</span> GitHub Issue Draft</h2>
{issue_draft_card}

<div class="section-divider"></div>
<h2 class="section-title"><span class="icon">🛠️</span> Proposed Fix Plan</h2>
{fix_plan_card}

<div class="report-footer">
  <p>Generated by Monkey Stress Tester · Treit/TerminalStress</p>
</div>

</div>
</body>
</html>
"""

    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"Report written to {OUTPUT_PATH}")

    if args.open:
        if sys.platform == "win32":
            os.startfile(str(OUTPUT_PATH))
        else:
            webbrowser.open(OUTPUT_PATH.as_uri())


if __name__ == "__main__":
    main()
