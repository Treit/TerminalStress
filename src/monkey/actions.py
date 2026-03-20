"""
Action catalog for Windows Terminal monkey testing.

Each action is a callable that operates on a running Windows Terminal instance.
Actions are weighted — higher weight means the action is selected more often.
"""

import ctypes
import ctypes.wintypes
import glob
import logging
import os
import random
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pywinauto
from pywinauto.keyboard import send_keys as _raw_send_keys
from pywinauto import mouse

from .input_lock import get_input_lock

logger = logging.getLogger("monkey")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Win32 constants for modifier key flushing
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002
WM_KEYDOWN = 0x0100

# The WT window handle, set by the runner before actions execute
_target_hwnd: int = 0
_target_pid: int = 0


def set_target_hwnd(hwnd: int, pid: int | None = None):
    """Set the target WT window handle and pid for focus verification."""
    global _target_hwnd, _target_pid
    _target_hwnd = hwnd
    if pid is not None:
        _target_pid = pid


def _get_window_pid(hwnd: int) -> int:
    """Return the pid owning a given hwnd, or 0 if unknown."""
    if not hwnd:
        return 0
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _get_window_class(hwnd: int) -> str:
    """Return the window class name for a given hwnd."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _flush_modifiers():
    """
    Send explicit key-up events for all modifier keys to clear any stuck state.
    This prevents stale ALT/CTRL/SHIFT/WIN keys from causing Start Menu or
    Alt+Tab activation.
    """
    for vk in (VK_MENU, VK_LMENU, VK_RMENU,
               VK_CONTROL, VK_LCONTROL, VK_RCONTROL,
               VK_SHIFT, VK_LSHIFT, VK_RSHIFT,
               VK_LWIN, VK_RWIN):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _is_target_foreground() -> bool:
    """
    Check whether the foreground window belongs to the target WT process.
    This allows focused child windows inside WT while still rejecting foreign apps.
    """
    foreground = user32.GetForegroundWindow()
    if _target_hwnd == 0 or foreground == 0:
        return False
    if foreground == _target_hwnd:
        return True
    return _target_pid != 0 and _get_window_pid(foreground) == _target_pid


def _assert_target_focus_stable(samples: int = 1, delay_s: float = 0.01):
    """Quick focus check. The keyboard hook now prevents most focus-stealing,
    so a single fast poll is sufficient."""
    for _ in range(samples):
        if not _is_target_foreground():
            raise FocusError("WT lost focus before input")
        time.sleep(delay_s)


def _safe_send_keys(keys: str, **kwargs):
    """
    Wrapper around send_keys that:
    1. Acquires the cross-process input lock (prevents interleaving with other instances)
    2. Flushes stale modifier key state
    3. Verifies WT is focused
    4. Sends the keys
    5. Flushes modifiers again as a safety net
    Raises FocusError if WT lost focus between action start and now.
    """
    lock = get_input_lock()
    with lock:
        _flush_modifiers()
        _assert_target_focus_stable()
        _raw_send_keys(keys, **kwargs)
        _flush_modifiers()


def _safe_click(coords):
    """Mouse click only if WT is focused, serialized via input lock."""
    lock = get_input_lock()
    with lock:
        _assert_target_focus_stable()
        mouse.click(coords=coords)


def _safe_mouse_press(coords):
    lock = get_input_lock()
    with lock:
        _assert_target_focus_stable()
        mouse.press(coords=coords)


def _safe_mouse_move(coords):
    lock = get_input_lock()
    with lock:
        _assert_target_focus_stable()
        mouse.move(coords=coords)


def _safe_mouse_release(coords):
    lock = get_input_lock()
    with lock:
        _assert_target_focus_stable()
        mouse.release(coords=coords)

# Timing constants
MIN_ACTION_DELAY = 0.005
MAX_ACTION_DELAY = 0.05
RESIZE_HOLD_REPEATS_MIN = 3
RESIZE_HOLD_REPEATS_MAX = 30


def _brief_sleep(max_ms: int = 150):
    """Sleep for a randomized short duration to let WT process input."""
    time.sleep(random.uniform(0.005, min(max_ms / 2000.0, 0.25)))

# Known-bug mitigations (overridden by known_bugs.json at catalog build time)
_mitigations: dict[str, object] = {}


ACTION_PROFILES: dict[str, dict[str, float]] = {
    "default": {},
    "all-surfaces": {
        "buffer": 2.5,
        "mouse": 2.0,
        "render": 2.0,
        "stress": 2.0,
        "ui": 2.5,
        "window": 2.0,
    },
    "buffer-chaos": {
        "buffer": 4.0,
        "layout": 1.5,
        "scroll": 2.5,
        "stress": 2.5,
    },
    "scroll-race": {
        "buffer": 1.5,
        "layout": 2.5,
        "navigation": 2.0,
        "scroll": 4.0,
    },
}


def load_known_bugs() -> dict[str, object]:
    """Load known_bugs.json and return a dict of active mitigations."""
    import json
    from pathlib import Path

    bugs_file = Path(__file__).parent / "known_bugs.json"
    mitigations = {}
    if bugs_file.exists():
        with open(bugs_file, "r") as f:
            data = json.load(f)
        for bug in data.get("bugs", []):
            if bug.get("enabled", True):
                mitigations[bug["mitigation"]] = bug["mitigation_value"]
                logger.info(
                    f"Known bug [{bug['id']}]: applying mitigation "
                    f"{bug['mitigation']}={bug['mitigation_value']}"
                )
    return mitigations


def _get_resize_repeats() -> int:
    """Get the number of resize repeats, capped by known-bug mitigations."""
    cap = _mitigations.get("max_pane_resize_repeats", RESIZE_HOLD_REPEATS_MAX)
    return random.randint(RESIZE_HOLD_REPEATS_MIN, min(RESIZE_HOLD_REPEATS_MAX, cap))


@dataclass(frozen=True)
class Action:
    name: str
    weight: int
    func: Callable
    tags: tuple[str, ...] = ()


class FocusError(Exception):
    """Raised when WT is not the foreground window and we can't fix it."""
    pass


def get_action_profiles() -> tuple[str, ...]:
    """Return the available action profile names."""
    return tuple(ACTION_PROFILES.keys())


def _profiled_action(
    name: str,
    weight: int,
    func: Callable,
    *tags: str,
    action_profile: str = "default",
) -> Action:
    """Create an action with profile-adjusted weight and code-path tags."""
    profile_multipliers = ACTION_PROFILES.get(action_profile, {})
    multiplier = 1.0
    for tag in tags:
        multiplier = max(multiplier, profile_multipliers.get(tag, 1.0))
    adjusted_weight = max(1, int(round(weight * multiplier)))
    return Action(name=name, weight=adjusted_weight, func=func, tags=tuple(tags))


# Classes for rogue foreground windows that steal focus
_ROGUE_WINDOW_CLASSES = frozenset({
    "Windows.UI.Core.CoreWindow",       # Start Menu
    "MultitaskingViewFrame",            # Alt+Tab task switcher
    "XamlExplorerHostIslandWindow",     # Start Menu (newer Windows builds)
    "Shell_TrayWnd",                    # Taskbar
})


def _dismiss_rogue_foreground():
    """
    If the foreground window is a known focus-stealer (Start Menu, Alt+Tab UI),
    dismiss it by sending ESC. Returns True if a rogue window was dismissed.
    """
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    cls = _get_window_class(fg)
    if cls in _ROGUE_WINDOW_CLASSES:
        logger.info(f"Dismissing rogue foreground window: class={cls}")
        user32.PostMessageW(fg, WM_KEYDOWN, VK_ESCAPE, 0)
        time.sleep(0.15)
        return True
    return False


def _ensure_focused(win):
    """
    Bring the WT window to the foreground and VERIFY it's actually focused.
    Raises FocusError if WT cannot be focused, preventing input from going
    to the wrong window.

    Always attempts to reclaim focus for WT via SetForegroundWindow.
    """
    hwnd = win.handle
    foreground = user32.GetForegroundWindow()
    foreground_pid = _get_window_pid(foreground)

    # If a rogue window (Start Menu, Alt+Tab) has focus, dismiss it first
    if foreground and foreground != hwnd and foreground_pid not in (0, _target_pid):
        _dismiss_rogue_foreground()

    # Try to bring WT to the foreground
    try:
        if not _is_target_foreground():
            fore_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            our_tid = kernel32.GetCurrentThreadId()
            if fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, True)
            user32.SetForegroundWindow(hwnd)
            if fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, False)
            _assert_target_focus_stable(samples=2, delay_s=0.01)
    except Exception:
        pass

    # Verify focus was actually acquired
    if not _is_target_foreground():
        raise FocusError("WT is not the foreground window, skipping action")


def split_pane_right(win):
    """Split the current pane to the right (Alt+Shift+=)."""
    _ensure_focused(win)
    _safe_send_keys("%+{=}")
    _brief_sleep(200)


def split_pane_down(win):
    """Split the current pane downward (Alt+Shift+-)."""
    _ensure_focused(win)
    _safe_send_keys("%+{-}")
    _brief_sleep(200)


def close_pane(win):
    """Close the current pane (Ctrl+Shift+W), but open a new tab first to prevent closing WT."""
    _ensure_focused(win)
    _safe_send_keys("^+t")
    _brief_sleep(200)
    _safe_send_keys("^+{TAB}")
    _brief_sleep(100)
    _safe_send_keys("^+w")
    _brief_sleep(150)


def resize_pane_left(win):
    """Shrink/grow pane to the left by holding Alt+Shift+Left."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{LEFT}")
        time.sleep(random.uniform(0.005, 0.03))


def resize_pane_right(win):
    """Grow/shrink pane to the right by holding Alt+Shift+Right."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{RIGHT}")
        time.sleep(random.uniform(0.005, 0.03))


def resize_pane_up(win):
    """Shrink/grow pane upward by holding Alt+Shift+Up."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{UP}")
        time.sleep(random.uniform(0.005, 0.03))


def resize_pane_down(win):
    """Grow/shrink pane downward by holding Alt+Shift+Down."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{DOWN}")
        time.sleep(random.uniform(0.005, 0.03))


def focus_pane_left(win):
    """Move focus to the left pane (Alt+Left)."""
    _ensure_focused(win)
    _safe_send_keys("%{LEFT}")
    _brief_sleep(50)


def focus_pane_right(win):
    """Move focus to the right pane (Alt+Right)."""
    _ensure_focused(win)
    _safe_send_keys("%{RIGHT}")
    _brief_sleep(50)


def focus_pane_up(win):
    """Move focus to the pane above (Alt+Up)."""
    _ensure_focused(win)
    _safe_send_keys("%{UP}")
    _brief_sleep(50)


def focus_pane_down(win):
    """Move focus to the pane below (Alt+Down)."""
    _ensure_focused(win)
    _safe_send_keys("%{DOWN}")
    _brief_sleep(50)


def new_tab(win):
    """Open a new tab (Ctrl+Shift+T)."""
    _ensure_focused(win)
    _safe_send_keys("^+t")
    _brief_sleep(300)


def close_tab(win):
    """Close the current tab, but open a new one first to prevent closing WT."""
    _ensure_focused(win)
    _safe_send_keys("^+t")
    _brief_sleep(200)
    _safe_send_keys("^+{TAB}")
    _brief_sleep(100)
    _safe_send_keys("^+w")
    _brief_sleep(150)


def next_tab(win):
    """Switch to the next tab (Ctrl+Tab)."""
    _ensure_focused(win)
    _safe_send_keys("^{TAB}")
    _brief_sleep(80)


def prev_tab(win):
    """Switch to the previous tab (Ctrl+Shift+Tab)."""
    _ensure_focused(win)
    _safe_send_keys("^+{TAB}")
    _brief_sleep(80)


def type_random_text(win):
    """Type a random string of safe characters (no Tab or keys that could switch focus)."""
    _ensure_focused(win)
    length = random.randint(1, 80)
    safe_chars = string.ascii_letters + string.digits + string.punctuation + " "
    text = "".join(random.choices(safe_chars, k=length))
    # Escape special pywinauto characters
    safe = text.replace("{", "{{").replace("}", "}}").replace("+", "{+}").replace("^", "{^}").replace("%", "{%}")
    try:
        _safe_send_keys(safe, pause=0.01)
    except Exception:
        pass
    _brief_sleep(50)


def type_enter(win):
    """Press Enter."""
    _ensure_focused(win)
    _safe_send_keys("{ENTER}")
    _brief_sleep(50)


def type_command(win):
    """Type a harmless shell command and press Enter."""
    _ensure_focused(win)
    commands = [
        "echo hello",
        "dir",
        "cls",
        "echo %RANDOM%",
        "hostname",
        "ver",
        "time /t",
        "date /t",
        "set",
        "color 0a",
        "color 07",
        "title MonkeyTest",
    ]
    cmd = random.choice(commands)
    _safe_send_keys(cmd + "{ENTER}", pause=0.02)
    _brief_sleep(100)


def clear_buffer(win):
    """Clear the terminal buffer via WT's shortcut (Ctrl+Shift+K)."""
    _ensure_focused(win)
    _safe_send_keys("^+k")
    _brief_sleep(150)


def scroll_up(win):
    """Scroll up in the terminal (Ctrl+Shift+Up or mouse wheel)."""
    _ensure_focused(win)
    repeats = random.randint(1, 20)
    for _ in range(repeats):
        _safe_send_keys("^+{UP}")
        time.sleep(0.005)


def scroll_down(win):
    """Scroll down in the terminal (Ctrl+Shift+Down or mouse wheel)."""
    _ensure_focused(win)
    repeats = random.randint(1, 20)
    for _ in range(repeats):
        _safe_send_keys("^+{DOWN}")
        time.sleep(0.005)


def resize_window(win):
    """Resize the Windows Terminal window to a random size."""
    _ensure_focused(win)
    try:
        new_width = random.randint(300, 2560)
        new_height = random.randint(200, 1440)
        new_x = random.randint(0, max(0, 1920 - new_width))
        new_y = random.randint(0, max(0, 1080 - new_height))
        user32.MoveWindow(win.handle, new_x, new_y, new_width, new_height, True)
    except Exception as e:
        logger.warning(f"resize_window failed: {e}")
    _brief_sleep(100)


def maximize_window(win):
    """Maximize the terminal window."""
    _ensure_focused(win)
    try:
        user32.ShowWindow(win.handle, 3)  # SW_MAXIMIZE
    except Exception:
        pass
    _brief_sleep(100)


def minimize_restore_window(win):
    """Minimize then restore the terminal window."""
    _ensure_focused(win)
    try:
        user32.ShowWindow(win.handle, 6)  # SW_MINIMIZE
        _brief_sleep(300)
        user32.ShowWindow(win.handle, 9)  # SW_RESTORE
        # Re-verify focus after restore
        time.sleep(0.1)
        user32.SetForegroundWindow(win.handle)
    except Exception:
        pass
    _brief_sleep(150)


def toggle_fullscreen(win):
    """Toggle fullscreen (F11)."""
    _ensure_focused(win)
    _safe_send_keys("{F11}")
    _brief_sleep(300)


def zoom_in(win):
    """Increase font size (Ctrl+=)."""
    _ensure_focused(win)
    repeats = random.randint(1, 5)
    for _ in range(repeats):
        _safe_send_keys("^{=}")
        time.sleep(0.01)


def zoom_out(win):
    """Decrease font size (Ctrl+-)."""
    _ensure_focused(win)
    repeats = random.randint(1, 5)
    for _ in range(repeats):
        _safe_send_keys("^{-}")
        time.sleep(0.01)


def zoom_reset(win):
    """Reset font size (Ctrl+0)."""
    _ensure_focused(win)
    _safe_send_keys("^0")
    _brief_sleep(50)


def open_search(win):
    """Open the search dialog (Ctrl+Shift+F)."""
    _ensure_focused(win)
    _safe_send_keys("^+f")
    _brief_sleep(200)
    text = "".join(random.choices(string.ascii_letters, k=random.randint(1, 20)))
    _safe_send_keys(text, pause=0.02)
    _brief_sleep(100)
    _safe_send_keys("{ESC}")
    _brief_sleep(50)


def mouse_click_random(win):
    """Click at a random position within the terminal window."""
    _ensure_focused(win)
    try:
        rect = win.rectangle()
        x = random.randint(rect.left + 10, max(rect.left + 11, rect.right - 10))
        y = random.randint(rect.top + 10, max(rect.top + 11, rect.bottom - 10))
        _safe_click(coords=(x, y))
    except Exception as e:
        logger.warning(f"mouse_click_random failed: {e}")
    _brief_sleep(50)


def mouse_drag_random(win):
    """Drag the mouse across a random region of the terminal (text selection)."""
    _ensure_focused(win)
    try:
        rect = win.rectangle()
        x1 = random.randint(rect.left + 10, max(rect.left + 11, rect.right - 10))
        y1 = random.randint(rect.top + 30, max(rect.top + 31, rect.bottom - 10))
        x2 = random.randint(rect.left + 10, max(rect.left + 11, rect.right - 10))
        y2 = random.randint(rect.top + 30, max(rect.top + 31, rect.bottom - 10))
        _safe_mouse_press(coords=(x1, y1))
        time.sleep(0.005)
        _safe_mouse_move(coords=(x2, y2))
        time.sleep(0.005)
        _safe_mouse_release(coords=(x2, y2))
    except Exception as e:
        logger.warning(f"mouse_drag_random failed: {e}")
    _brief_sleep(50)


def copy_paste(win):
    """Copy selection then paste (Ctrl+Shift+C, Ctrl+Shift+V)."""
    _ensure_focused(win)
    _safe_send_keys("^+c")
    _brief_sleep(50)
    _safe_send_keys("^+v")
    _brief_sleep(50)


def open_settings(win):
    """Open settings and immediately close (Ctrl+,)."""
    _ensure_focused(win)
    _safe_send_keys("^{,}")
    _brief_sleep(500)
    _safe_send_keys("^+w")
    _brief_sleep(150)


def open_command_palette(win):
    """Open command palette (Ctrl+Shift+P) and dismiss."""
    _ensure_focused(win)
    _safe_send_keys("^+p")
    _brief_sleep(300)
    _safe_send_keys("{ESC}")
    _brief_sleep(100)


def command_palette_search(win):
    """
    Open command palette, type a partial search term to filter commands,
    then dismiss. Does NOT press Enter to avoid executing dangerous commands.
    """
    _ensure_focused(win)
    terms = [
        "split", "pane", "font", "size", "color",
        "theme", "scroll", "copy", "paste", "find",
        "zoom", "focus", "new", "rename",
    ]
    term = random.choice(terms)
    _safe_send_keys("^+p")
    _brief_sleep(200)
    _safe_send_keys(term, pause=0.02)
    _brief_sleep(150)
    _safe_send_keys("{ESC}")
    logger.info(f"Command palette: searched '{term}', dismissed")
    _brief_sleep(100)


# Track the TerminalStress subprocess so we don't launch multiples
_stress_proc: subprocess.Popen | None = None

# Path to the TerminalStress project
_STRESS_CSPROJ = Path(__file__).parent.parent / "TerminalStress.csproj"


def _find_stress_exe() -> str | None:
    """Find a pre-built TerminalStress.exe under bin/, or None."""
    bin_dir = Path(__file__).parent.parent / "bin"
    matches = list(bin_dir.rglob("TerminalStress.exe")) if bin_dir.exists() else []
    return str(matches[0]) if matches else None


def run_terminal_stress(win):
    """
    Launch TerminalStress in the focused pane.
    Uses the pre-built exe if it exists under bin/, otherwise uses dotnet run.
    Types the command into the current pane and presses Enter.
    """
    global _stress_proc
    _ensure_focused(win)

    exe = _find_stress_exe()
    if exe:
        cmd = exe.replace("\\", "\\\\")
    else:
        cmd = f"dotnet run --project {str(_STRESS_CSPROJ)}"

    # Type the command into the focused terminal pane
    _safe_send_keys(cmd + "{ENTER}", pause=0.02)
    logger.info(f"Launched TerminalStress via: {cmd}")
    _brief_sleep(500)


def stop_terminal_stress(win):
    """Send Ctrl+C to the focused pane to stop a running TerminalStress."""
    _ensure_focused(win)
    _safe_send_keys("^c")
    _brief_sleep(200)


# Actions that are inherently unsafe in multi-instance mode because they
# deliberately cause focus loss or window state transitions that confuse
# concurrent instances.
_MULTI_INSTANCE_DISABLED = frozenset({
    "minimize_restore_window",  # deliberately minimizes the window
    "toggle_fullscreen",        # F11 transitions can cause focus loss
})


# Build the action catalog with weights.
# Higher weight = more likely to be selected.
# Pane resize actions have the highest weight since that's where bugs tend to hide.
def build_action_catalog(action_profile: str = "default", multi_instance: bool = False) -> list[Action]:
    global _mitigations
    _mitigations = load_known_bugs()

    catalog = [
        # Pane resize (high weight — this is where bugs like #7416 and #19996 live)
        _profiled_action("resize_pane_left", 15, resize_pane_left, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_right", 15, resize_pane_right, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_up", 10, resize_pane_up, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_down", 10, resize_pane_down, "layout", action_profile=action_profile),

        # Pane management
        _profiled_action("split_pane_right", 12, split_pane_right, "layout", "navigation", action_profile=action_profile),
        _profiled_action("split_pane_down", 12, split_pane_down, "layout", "navigation", action_profile=action_profile),
        _profiled_action("close_pane", 6, close_pane, "layout", "navigation", action_profile=action_profile),

        # Pane focus
        _profiled_action("focus_pane_left", 8, focus_pane_left, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_right", 8, focus_pane_right, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_up", 4, focus_pane_up, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_down", 4, focus_pane_down, "navigation", action_profile=action_profile),

        # Tab management
        _profiled_action("new_tab", 3, new_tab, "navigation", "layout", action_profile=action_profile),
        _profiled_action("close_tab", 2, close_tab, "navigation", "layout", action_profile=action_profile),
        _profiled_action("next_tab", 3, next_tab, "navigation", action_profile=action_profile),
        _profiled_action("prev_tab", 3, prev_tab, "navigation", action_profile=action_profile),

        # Typing
        _profiled_action("type_random_text", 5, type_random_text, "input", action_profile=action_profile),
        _profiled_action("type_enter", 5, type_enter, "input", action_profile=action_profile),
        _profiled_action("type_command", 4, type_command, "buffer", "input", action_profile=action_profile),
        _profiled_action("clear_buffer", 5, clear_buffer, "buffer", "input", action_profile=action_profile),

        # Scrolling
        _profiled_action("scroll_up", 4, scroll_up, "navigation", "scroll", action_profile=action_profile),
        _profiled_action("scroll_down", 4, scroll_down, "navigation", "scroll", action_profile=action_profile),

        # Window management
        _profiled_action("resize_window", 6, resize_window, "render", "window", action_profile=action_profile),
        _profiled_action("maximize_window", 2, maximize_window, "window", action_profile=action_profile),
        _profiled_action("minimize_restore_window", 1, minimize_restore_window, "window", action_profile=action_profile),
        _profiled_action("toggle_fullscreen", 1, toggle_fullscreen, "render", "window", action_profile=action_profile),

        # Zoom
        _profiled_action("zoom_in", 2, zoom_in, "render", action_profile=action_profile),
        _profiled_action("zoom_out", 2, zoom_out, "render", action_profile=action_profile),
        _profiled_action("zoom_reset", 2, zoom_reset, "render", action_profile=action_profile),

        # Search & UI
        _profiled_action("open_search", 2, open_search, "input", "ui", action_profile=action_profile),
        _profiled_action("open_command_palette", 1, open_command_palette, "ui", action_profile=action_profile),
        _profiled_action("command_palette_search", 3, command_palette_search, "input", "ui", action_profile=action_profile),
        _profiled_action("open_settings", 1, open_settings, "render", "ui", action_profile=action_profile),

        # Mouse
        _profiled_action("mouse_click_random", 4, mouse_click_random, "mouse", action_profile=action_profile),
        _profiled_action("mouse_drag_random", 2, mouse_drag_random, "mouse", action_profile=action_profile),

        # Clipboard
        _profiled_action("copy_paste", 2, copy_paste, "buffer", "mouse", action_profile=action_profile),

        # TerminalStress
        _profiled_action("run_terminal_stress", 2, run_terminal_stress, "input", "stress", action_profile=action_profile),
        _profiled_action("stop_terminal_stress", 1, stop_terminal_stress, "input", "stress", action_profile=action_profile),
    ]

    if multi_instance:
        removed = [a.name for a in catalog if a.name in _MULTI_INSTANCE_DISABLED]
        catalog = [a for a in catalog if a.name not in _MULTI_INSTANCE_DISABLED]
        if removed:
            logger.info(
                f"Multi-instance mode: disabled focus-losing actions: {removed}"
            )

    return catalog


def pick_action(
    catalog: list[Action],
    recent_actions: tuple[str, ...] | None = None,
    recent_tags: tuple[str, ...] | None = None,
) -> Action:
    """Select a weighted-random action while nudging toward fresh code paths."""
    recent_action_set = set(recent_actions or ())
    recent_tag_set = set(recent_tags or ())
    adjusted_weights: list[float] = []

    for action in catalog:
        weight = float(action.weight)

        if action.name in recent_action_set:
            weight *= 0.2

        overlap = sum(1 for tag in action.tags if tag in recent_tag_set)
        if recent_tag_set:
            if overlap == 0:
                weight *= 1.5
            else:
                weight /= 1.0 + (0.35 * overlap)

        adjusted_weights.append(max(weight, 0.1))

    return random.choices(catalog, weights=adjusted_weights, k=1)[0]
