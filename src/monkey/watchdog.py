"""
Watchdog for monitoring Windows Terminal health.

Detects:
- Process crashes (process no longer running)
- UI hangs (window not responding to messages)
- Memory leaks (RSS growing beyond threshold over time)
"""

import ctypes
import ctypes.wintypes
import logging
import os
import time
from dataclasses import dataclass, field

import psutil

logger = logging.getLogger("monkey")

# Win32 constants
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000
PROCESS_QUERY_INFORMATION = 0x0400

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


@dataclass
class HealthSnapshot:
    timestamp: float
    pid: int
    is_running: bool
    is_responding: bool
    memory_rss_mb: float
    memory_private_mb: float
    cpu_percent: float


@dataclass
class WatchdogState:
    pid: int
    start_time: float = field(default_factory=time.time)
    snapshots: list[HealthSnapshot] = field(default_factory=list)
    initial_rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    hang_count: int = 0
    crash_detected: bool = False


class Watchdog:
    def __init__(self, pid: int, memory_threshold_mb: float = 2048.0, hang_timeout_ms: int = 5000):
        self.state = WatchdogState(pid=pid)
        self.memory_threshold_mb = memory_threshold_mb
        self.hang_timeout_ms = hang_timeout_ms
        self._process: psutil.Process | None = None
        self._hwnd: int = 0

        try:
            self._process = psutil.Process(pid)
            mem = self._process.memory_info()
            self.state.initial_rss_mb = mem.rss / (1024 * 1024)
            self.state.peak_rss_mb = self.state.initial_rss_mb
        except psutil.NoSuchProcess:
            self.state.crash_detected = True

    def set_hwnd(self, hwnd: int):
        """Set the window handle for hang detection."""
        self._hwnd = hwnd

    def is_process_running(self) -> bool:
        """Check if the target process is still alive."""
        try:
            return self._process is not None and self._process.is_running() and self._process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_exit_code(self) -> int | None:
        """Get the process exit code, or None if still running."""
        try:
            if self._process and not self._process.is_running():
                return self._process.wait(timeout=0)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
            pass
        return None

    def is_window_responding(self) -> bool:
        """
        Check if the window is responding to messages.
        Uses SendMessageTimeout with SMTO_ABORTIFHUNG to detect hung windows.
        """
        if not self._hwnd:
            return True  # Can't check without a handle, assume OK

        result = ctypes.wintypes.DWORD()
        ret = user32.SendMessageTimeoutW(
            self._hwnd,
            WM_NULL,
            0,
            0,
            SMTO_ABORTIFHUNG,
            self.hang_timeout_ms,
            ctypes.byref(result),
        )
        return ret != 0

    def take_snapshot(self) -> HealthSnapshot:
        """Take a health snapshot of the target process."""
        is_running = self.is_process_running()
        is_responding = self.is_window_responding() if is_running else False

        memory_rss_mb = 0.0
        memory_private_mb = 0.0
        cpu_percent = 0.0

        if is_running and self._process:
            try:
                mem = self._process.memory_info()
                memory_rss_mb = mem.rss / (1024 * 1024)
                memory_private_mb = getattr(mem, "private", mem.rss) / (1024 * 1024)
                cpu_percent = self._process.cpu_percent(interval=0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                is_running = False

        snapshot = HealthSnapshot(
            timestamp=time.time(),
            pid=self.state.pid,
            is_running=is_running,
            is_responding=is_responding,
            memory_rss_mb=memory_rss_mb,
            memory_private_mb=memory_private_mb,
            cpu_percent=cpu_percent,
        )

        self.state.snapshots.append(snapshot)

        if memory_rss_mb > self.state.peak_rss_mb:
            self.state.peak_rss_mb = memory_rss_mb

        if not is_running:
            self.state.crash_detected = True

        if not is_responding and is_running:
            self.state.hang_count += 1

        return snapshot

    def check_memory_leak(self) -> tuple[bool, float]:
        """
        Check if memory usage suggests a leak.
        Returns (is_leaking, growth_mb).
        """
        if len(self.state.snapshots) < 2:
            return False, 0.0

        recent = self.state.snapshots[-1]
        growth = recent.memory_rss_mb - self.state.initial_rss_mb
        is_leaking = recent.memory_rss_mb > self.memory_threshold_mb
        return is_leaking, growth

    def get_summary(self) -> dict:
        """Return a summary of the watchdog state."""
        duration = time.time() - self.state.start_time
        is_leaking, growth = self.check_memory_leak()
        return {
            "pid": self.state.pid,
            "duration_seconds": round(duration, 1),
            "is_running": self.is_process_running(),
            "crash_detected": self.state.crash_detected,
            "hang_count": self.state.hang_count,
            "initial_rss_mb": round(self.state.initial_rss_mb, 1),
            "peak_rss_mb": round(self.state.peak_rss_mb, 1),
            "current_rss_mb": round(self.state.snapshots[-1].memory_rss_mb, 1) if self.state.snapshots else 0,
            "memory_growth_mb": round(growth, 1),
            "possible_leak": is_leaking,
            "snapshots_taken": len(self.state.snapshots),
        }


def find_wt_process() -> psutil.Process | None:
    """Find the Windows Terminal process."""
    for proc in psutil.process_iter(["name", "pid"]):
        if proc.info["name"] and proc.info["name"].lower() == "windowsterminal.exe":
            return proc
    return None
