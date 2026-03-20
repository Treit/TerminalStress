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

logger = logging.getLogger("monkey")

user32 = ctypes.windll.user32
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
ASFW_ANY = -1

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


def _assert_target_focus_stable(samples: int = 2, delay_s: float = 0.005):
    """Quick check that WT still has focus."""
    for _ in range(samples):
        if not _is_target_foreground():
            raise FocusError("WT lost focus before input")
        time.sleep(delay_s)


def _reclaim_focus(win) -> bool:
    """Aggressively restore focus to WT before sending input."""
    hwnd = win.handle
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass

    for _ in range(3):
        foreground = user32.GetForegroundWindow()
        fore_tid = user32.GetWindowThreadProcessId(foreground, None)
        our_tid = ctypes.windll.kernel32.GetCurrentThreadId()

        try:
            try:
                user32.AllowSetForegroundWindow(ASFW_ANY)
            except Exception:
                pass
            try:
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

            if fore_tid and fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, True)
            if fore_tid and target_tid and fore_tid != target_tid:
                user32.AttachThreadInput(target_tid, fore_tid, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if fore_tid and fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, False)
            if fore_tid and target_tid and fore_tid != target_tid:
                user32.AttachThreadInput(target_tid, fore_tid, False)

        time.sleep(0.05)
        if _is_target_foreground():
            return True

        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(0.05)
        if _is_target_foreground():
            return True

        try:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.05)
        if _is_target_foreground():
            return True

        try:
            rect = win.rectangle()
            focus_x = max(rect.left + 40, rect.left + 5)
            focus_y = max(rect.top + 80, rect.top + 5)
            win.click_input(coords=(focus_x - rect.left, focus_y - rect.top))
        except Exception:
            pass
        time.sleep(0.05)
        if _is_target_foreground():
            return True

    return False


def _safe_send_keys(keys: str, **kwargs):
    """
    Wrapper around send_keys that verifies WT is focused immediately before
    sending. Raises FocusError if WT lost focus between action start and now.
    """
    _assert_target_focus_stable()
    # pywinauto's parse_keys silently strips literal spaces —
    # replace them with {SPACE} so they actually get typed.
    keys = keys.replace(" ", "{SPACE}")
    _raw_send_keys(keys, **kwargs)


def _paste_text(text: str):
    """
    Type text by copying to clipboard and pasting with Ctrl+Shift+V.
    Much faster than send_keys character-by-character.
    """
    import subprocess
    # Use clip.exe to set clipboard
    proc = subprocess.Popen(
        ["clip.exe"], stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    proc.communicate(input=text.encode("utf-16-le"))
    _assert_target_focus_stable()
    # Ctrl+Shift+V = WT paste
    _raw_send_keys("^+v")


def _paste_command(text: str):
    """Clear line, paste a command via clipboard, then press Enter."""
    _clear_input_line()
    _paste_text(text)
    time.sleep(0.02)
    _raw_send_keys("{ENTER}")


def _safe_click(coords):
    """Mouse click only if WT is focused."""
    _assert_target_focus_stable()
    mouse.click(coords=coords)


def _safe_mouse_press(coords):
    _assert_target_focus_stable()
    mouse.press(coords=coords)


def _safe_mouse_move(coords):
    _assert_target_focus_stable()
    mouse.move(coords=coords)


def _safe_mouse_release(coords):
    _assert_target_focus_stable()
    mouse.release(coords=coords)

# Timing constants
MIN_ACTION_DELAY = 0.005
MAX_ACTION_DELAY = 0.08
RESIZE_HOLD_REPEATS_MIN = 3
RESIZE_HOLD_REPEATS_MAX = 30


def _brief_sleep(max_ms: int = 150):
    """Sleep for a randomized short duration."""
    time.sleep(random.uniform(0.005, min(max_ms / 1000.0, 0.15)))


def _clear_input_line():
    """Send ESC + Ctrl+C to discard any partial input on the command line."""
    _raw_send_keys("{ESC}")
    time.sleep(0.015)
    _raw_send_keys("^c")
    time.sleep(0.03)

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


def _ensure_focused(win):
    """
    Bring the WT window to the foreground and VERIFY it's actually focused.
    Raises FocusError if WT cannot be focused, preventing input from going
    to the wrong window.
    """
    if _is_target_foreground():
        _assert_target_focus_stable(samples=2, delay_s=0.005)
        return

    if not _reclaim_focus(win):
        raise FocusError("Failed to restore focus to WT")

    _assert_target_focus_stable(samples=2, delay_s=0.005)


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
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_right(win):
    """Grow/shrink pane to the right by holding Alt+Shift+Right."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{RIGHT}")
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_up(win):
    """Shrink/grow pane upward by holding Alt+Shift+Up."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{UP}")
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_down(win):
    """Grow/shrink pane downward by holding Alt+Shift+Down."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        _safe_send_keys("%+{DOWN}")
        time.sleep(random.uniform(0.02, 0.08))


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
    """Type a random string of safe characters, then press Escape to avoid contaminating the next command."""
    _ensure_focused(win)
    length = random.randint(1, 40)
    safe_chars = string.ascii_letters + string.digits + " "
    text = "".join(random.choices(safe_chars, k=length))
    try:
        _safe_send_keys(text, pause=0.01)
    except Exception:
        pass
    try:
        _clear_input_line()
    except Exception:
        pass
    _brief_sleep(20)


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
    _paste_command(cmd)
    _brief_sleep(50)


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
        time.sleep(0.02)


def scroll_down(win):
    """Scroll down in the terminal (Ctrl+Shift+Down or mouse wheel)."""
    _ensure_focused(win)
    repeats = random.randint(1, 20)
    for _ in range(repeats):
        _safe_send_keys("^+{DOWN}")
        time.sleep(0.02)


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
        time.sleep(0.05)


def zoom_out(win):
    """Decrease font size (Ctrl+-)."""
    _ensure_focused(win)
    repeats = random.randint(1, 5)
    for _ in range(repeats):
        _safe_send_keys("^{-}")
        time.sleep(0.05)


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
        time.sleep(0.02)
        _safe_mouse_move(coords=(x2, y2))
        time.sleep(0.02)
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


# ---------------------------------------------------------------------------
# Shell-aware profiles and commands
# ---------------------------------------------------------------------------

# Profiles available for random tab/pane creation (subset of common shells)
_WT_PROFILES = [
    "Windows PowerShell",
    "Command Prompt",
    "PowerShell",
    "Git Bash",
]

# Read-only, non-destructive commands per shell family
_SHELL_COMMANDS: dict[str, list[str]] = {
    "pwsh": [
        "Get-ChildItem -Recurse -Depth 2 | Select-Object -First 50",
        "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10",
        "Get-Service | Where-Object Status -eq Running | Select-Object -First 20",
        "Get-Date -Format o",
        "$PSVersionTable",
        "[System.Environment]::OSVersion",
        "Get-History",
        "Get-PSDrive",
        "Get-Variable | Select-Object -First 30",
        "Get-Module -ListAvailable | Select-Object -First 15",
        "Get-EventLog -LogName Application -Newest 5 -ErrorAction SilentlyContinue",
        "1..100 | ForEach-Object { Write-Host ('x' * (Get-Random -Max 120)) }",
        "cls",
        "Clear-Host",
        "rg --stats -c 'class' C:\\Windows\\System32 2>$null | Select-Object -First 20",
        "Get-ChildItem C:\\ -Recurse -Depth 3 -ErrorAction SilentlyContinue | Measure-Object",
        "Get-ChildItem C:\\Windows\\System32\\*.dll | Sort-Object Length -Descending | Select-Object -First 20",
        "Get-Content $env:windir\\System32\\drivers\\etc\\hosts",
        "[System.IO.Directory]::GetFiles('C:\\Windows\\System32', '*.exe') | Select-Object -First 30",
    ],
    "cmd": [
        "dir /s /b C:\\Windows\\System32\\*.dll | findstr /c:terminal 2>nul",
        "dir",
        "echo %TIME% %DATE%",
        "hostname",
        "ver",
        "time /t",
        "date /t",
        "set",
        "echo %RANDOM%%RANDOM%%RANDOM%",
        "title MonkeyTest",
        "color 0a",
        "color 07",
        "cls",
        "type nul",
        "tree /f . | more",
        "systeminfo | findstr /c:OS",
        "dir /s /b C:\\ 2>nul | findstr /i terminal",
        "dir /s C:\\Windows\\System32\\*.exe 2>nul | more",
        "findstr /s /i /c:\"error\" C:\\Windows\\Logs\\*.log 2>nul | more",
        "wmic process list brief",
    ],
    "bash": [
        "ls -laR /tmp 2>/dev/null | head -50",
        "find /tmp -maxdepth 2 2>/dev/null | head -30",
        "echo $SHELL $BASH_VERSION",
        "cat /dev/null",
        "uname -a",
        "env | head -20",
        "ps aux | head -15",
        "date",
        "pwd",
        "history | tail -10",
        "seq 1 100 | xargs -I{} echo line{}",
        "clear",
        "find / -maxdepth 3 -name '*.exe' 2>/dev/null | head -20",
        "ls -laR /mnt 2>/dev/null | head -100",
        "cat /proc/version 2>/dev/null || echo not linux",
    ],
}

# Map profile names to shell family
_PROFILE_SHELL_MAP: dict[str, str] = {
    "Windows PowerShell": "pwsh",
    "PowerShell": "pwsh",
    "Developer PowerShell for VS 2022": "pwsh",
    "Developer PowerShell for VS 2022 (2)": "pwsh",
    "Developer PowerShell for VS 18": "pwsh",
    "Command Prompt": "cmd",
    "Developer Command Prompt for VS 2022": "cmd",
    "Developer Command Prompt for VS 2022 (2)": "cmd",
    "Developer Command Prompt for VS 18": "cmd",
    "Git Bash": "bash",
}

# Track which shell the most recent pane was opened with
_current_shell: str = "cmd"


def _set_current_shell(profile_name: str):
    """Update tracked shell family based on the profile that was opened."""
    global _current_shell
    _current_shell = _PROFILE_SHELL_MAP.get(profile_name, "cmd")


def _get_available_profiles() -> list[str]:
    """Return profiles that actually exist on this system."""
    try:
        settings_path = Path(os.environ.get("LOCALAPPDATA", "")) / \
            "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / \
            "LocalState" / "settings.json"
        if settings_path.exists():
            import json
            with open(settings_path) as f:
                data = json.load(f)
            names = {p.get("name", "") for p in data.get("profiles", {}).get("list", [])}
            return [p for p in _WT_PROFILES if p in names]
    except Exception:
        pass
    return _WT_PROFILES


def new_tab_profile(win):
    """Open a new tab with a randomly selected WT profile via wt.exe CLI."""
    _ensure_focused(win)
    profiles = _get_available_profiles()
    profile = random.choice(profiles)
    # wt.exe -w 0 new-tab -p "Profile Name" opens a tab in the current window
    subprocess.Popen(
        ["wt.exe", "-w", "0", "new-tab", "-p", profile],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _set_current_shell(profile)
    logger.info(f"new_tab_profile: opened '{profile}' (shell={_current_shell})")
    _brief_sleep(150)


def split_pane_right_profile(win):
    """Split pane right with a randomly selected WT profile via wt.exe CLI."""
    _ensure_focused(win)
    profiles = _get_available_profiles()
    profile = random.choice(profiles)
    subprocess.Popen(
        ["wt.exe", "-w", "0", "split-pane", "-H", "-p", profile],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _set_current_shell(profile)
    logger.info(f"split_pane_right_profile: opened '{profile}' (shell={_current_shell})")
    _brief_sleep(150)


def split_pane_down_profile(win):
    """Split pane down with a randomly selected WT profile via wt.exe CLI."""
    _ensure_focused(win)
    profiles = _get_available_profiles()
    profile = random.choice(profiles)
    subprocess.Popen(
        ["wt.exe", "-w", "0", "split-pane", "-V", "-p", profile],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _set_current_shell(profile)
    logger.info(f"split_pane_down_profile: opened '{profile}' (shell={_current_shell})")
    _brief_sleep(150)


def type_shell_command(win):
    """Type a shell-appropriate command based on the current pane's shell."""
    _ensure_focused(win)
    commands = _SHELL_COMMANDS.get(_current_shell, _SHELL_COMMANDS["cmd"])
    cmd = random.choice(commands)
    _paste_command(cmd)
    logger.info(f"type_shell_command [{_current_shell}]: {cmd}")
    _brief_sleep(50)


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
    """
    global _stress_proc
    _ensure_focused(win)

    exe = _find_stress_exe()
    if exe:
        cmd = exe
    else:
        cmd = f"dotnet run --project {str(_STRESS_CSPROJ)}"

    _paste_command(cmd)
    logger.info(f"Launched TerminalStress via: {cmd}")
    _brief_sleep(150)


def stop_terminal_stress(win):
    """Send Ctrl+C to the focused pane to stop a running TerminalStress."""
    _ensure_focused(win)
    _safe_send_keys("^c")
    _brief_sleep(200)


# Microsoft Edit (edit.exe) — a TUI editor that stresses rendering
_edit_exe: str | None = None


def _find_edit_exe() -> str | None:
    """Find Microsoft Edit (edit.exe) on the system, or None."""
    global _edit_exe
    if _edit_exe is not None:
        return _edit_exe or None
    import shutil
    found = shutil.which("edit")
    if found:
        _edit_exe = found
        return found
    # Check WinGet install path
    winget_path = Path(os.environ.get("LOCALAPPDATA", "")) / \
        "Microsoft" / "WinGet" / "Packages"
    if winget_path.exists():
        matches = list(winget_path.rglob("edit.exe"))
        if matches:
            _edit_exe = str(matches[0])
            return _edit_exe
    _edit_exe = ""
    return None


def run_edit(win):
    """Launch Microsoft Edit (edit.exe) in the focused pane to stress TUI rendering."""
    _ensure_focused(win)
    exe = _find_edit_exe()
    if not exe:
        logger.debug("run_edit: edit.exe not found, skipping")
        return
    targets = ["", ".", "$env:TEMP\\monkey_scratch.txt"]
    target = random.choice(targets)
    cmd = f"edit {target}".strip()
    _paste_command(cmd)
    logger.info(f"Launched edit.exe: {cmd}")
    _brief_sleep(150)


def stress_edit(win):
    """
    Send random keystrokes into a running edit.exe session — cursor movement,
    typing, scrolling, search. Exercises TUI rendering paths in the terminal.
    """
    _ensure_focused(win)
    actions = [
        # Cursor movement
        lambda: _safe_send_keys("{UP}" * random.randint(1, 20), pause=0.01),
        lambda: _safe_send_keys("{DOWN}" * random.randint(1, 20), pause=0.01),
        lambda: _safe_send_keys("{LEFT}" * random.randint(1, 10), pause=0.01),
        lambda: _safe_send_keys("{RIGHT}" * random.randint(1, 10), pause=0.01),
        lambda: _safe_send_keys("{PGUP}"),
        lambda: _safe_send_keys("{PGDN}"),
        lambda: _safe_send_keys("{HOME}"),
        lambda: _safe_send_keys("{END}"),
        lambda: _safe_send_keys("^{HOME}"),
        lambda: _safe_send_keys("^{END}"),
        # Type random text
        lambda: _safe_send_keys(
            "".join(random.choices(string.ascii_letters + string.digits + " ", k=random.randint(5, 40))),
            pause=0.01,
        ),
        lambda: _safe_send_keys("{ENTER}"),
        # Search (Ctrl+F in edit)
        lambda: [
            _safe_send_keys("^f"),
            _brief_sleep(100),
            _safe_send_keys("".join(random.choices(string.ascii_lowercase, k=3)), pause=0.02),
            _brief_sleep(100),
            _safe_send_keys("{ENTER}"),
            _brief_sleep(50),
            _safe_send_keys("{ESC}"),
        ],
        # Select text
        lambda: _safe_send_keys("+{DOWN}" * random.randint(1, 5), pause=0.01),
        lambda: _safe_send_keys("+{RIGHT}" * random.randint(1, 15), pause=0.01),
    ]
    action = random.choice(actions)
    try:
        result = action()
    except Exception:
        pass
    _brief_sleep(30)


def stop_edit(win):
    """Exit edit.exe without saving (Ctrl+Q or Ctrl+C)."""
    _ensure_focused(win)
    _safe_send_keys("^q")
    _brief_sleep(100)
    # Dismiss any "save?" prompt
    _safe_send_keys("n")
    _brief_sleep(100)


# Build the action catalog with weights.
# Higher weight = more likely to be selected.
# Pane resize actions have the highest weight since that's where bugs tend to hide.
def build_action_catalog(action_profile: str = "default") -> list[Action]:
    global _mitigations
    _mitigations = load_known_bugs()

    return [
        # Pane resize (high weight — this is where bugs like #7416 and #19996 live)
        _profiled_action("resize_pane_left", 15, resize_pane_left, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_right", 15, resize_pane_right, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_up", 10, resize_pane_up, "layout", action_profile=action_profile),
        _profiled_action("resize_pane_down", 10, resize_pane_down, "layout", action_profile=action_profile),

        # Pane management
        _profiled_action("split_pane_right", 6, split_pane_right, "layout", "navigation", action_profile=action_profile),
        _profiled_action("split_pane_down", 6, split_pane_down, "layout", "navigation", action_profile=action_profile),
        _profiled_action("split_pane_right_profile", 6, split_pane_right_profile, "layout", "navigation", action_profile=action_profile),
        _profiled_action("split_pane_down_profile", 6, split_pane_down_profile, "layout", "navigation", action_profile=action_profile),
        _profiled_action("close_pane", 6, close_pane, "layout", "navigation", action_profile=action_profile),

        # Pane focus
        _profiled_action("focus_pane_left", 8, focus_pane_left, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_right", 8, focus_pane_right, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_up", 4, focus_pane_up, "navigation", action_profile=action_profile),
        _profiled_action("focus_pane_down", 4, focus_pane_down, "navigation", action_profile=action_profile),

        # Tab management
        _profiled_action("new_tab", 2, new_tab, "navigation", "layout", action_profile=action_profile),
        _profiled_action("new_tab_profile", 3, new_tab_profile, "navigation", "layout", action_profile=action_profile),
        _profiled_action("close_tab", 2, close_tab, "navigation", "layout", action_profile=action_profile),
        _profiled_action("next_tab", 3, next_tab, "navigation", action_profile=action_profile),
        _profiled_action("prev_tab", 3, prev_tab, "navigation", action_profile=action_profile),

        # Typing
        _profiled_action("type_random_text", 2, type_random_text, "input", action_profile=action_profile),
        _profiled_action("type_enter", 3, type_enter, "input", action_profile=action_profile),
        _profiled_action("type_command", 2, type_command, "buffer", "input", action_profile=action_profile),
        _profiled_action("type_shell_command", 5, type_shell_command, "buffer", "input", action_profile=action_profile),
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

        # Microsoft Edit (TUI rendering stress)
        _profiled_action("run_edit", 2, run_edit, "input", "render", "stress", action_profile=action_profile),
        _profiled_action("stress_edit", 3, stress_edit, "input", "render", action_profile=action_profile),
        _profiled_action("stop_edit", 1, stop_edit, "input", "stress", action_profile=action_profile),
    ]


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
