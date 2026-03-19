"""
Action catalog for Windows Terminal monkey testing.

Each action is a callable that operates on a running Windows Terminal instance.
Actions are weighted — higher weight means the action is selected more often.
"""

import ctypes
import glob
import os
import random
import string
import subprocess
import time
import logging
from pathlib import Path
from typing import Callable, NamedTuple

import pywinauto
from pywinauto.keyboard import send_keys
from pywinauto import mouse

logger = logging.getLogger("monkey")

user32 = ctypes.windll.user32

# Timing constants
MIN_ACTION_DELAY = 0.01
MAX_ACTION_DELAY = 0.15
RESIZE_HOLD_REPEATS_MIN = 3
RESIZE_HOLD_REPEATS_MAX = 30


def _brief_sleep(max_ms: int = 150):
    """Sleep for a randomized short duration, never more than 0.5s."""
    time.sleep(random.uniform(0.01, min(max_ms / 1000.0, 0.5)))

# Known-bug mitigations (overridden by known_bugs.json at catalog build time)
_mitigations: dict[str, object] = {}


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


class Action(NamedTuple):
    name: str
    weight: int
    func: Callable


class FocusError(Exception):
    """Raised when WT is not the foreground window and we can't fix it."""
    pass


def _ensure_focused(win):
    """
    Bring the WT window to the foreground and VERIFY it's actually focused.
    Raises FocusError if WT cannot be focused, preventing input from going
    to the wrong window.
    """
    hwnd = win.handle
    try:
        if user32.GetForegroundWindow() != hwnd:
            fore_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, True)
            user32.SetForegroundWindow(hwnd)
            if fore_tid != our_tid:
                user32.AttachThreadInput(our_tid, fore_tid, False)
            time.sleep(0.05)
    except Exception:
        pass

    # Verify focus was actually acquired
    if user32.GetForegroundWindow() != hwnd:
        raise FocusError("WT is not the foreground window, skipping action")


def split_pane_right(win):
    """Split the current pane to the right (Alt+Shift+=)."""
    _ensure_focused(win)
    send_keys("%+{=}")
    _brief_sleep(200)


def split_pane_down(win):
    """Split the current pane downward (Alt+Shift+-)."""
    _ensure_focused(win)
    send_keys("%+{-}")
    _brief_sleep(200)


def close_pane(win):
    """Close the current pane (Ctrl+Shift+W), but open a new tab first to prevent closing WT."""
    _ensure_focused(win)
    send_keys("^+t")
    _brief_sleep(200)
    send_keys("^+{TAB}")
    _brief_sleep(100)
    send_keys("^+w")
    _brief_sleep(150)


def resize_pane_left(win):
    """Shrink/grow pane to the left by holding Alt+Shift+Left."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        send_keys("%+{LEFT}")
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_right(win):
    """Grow/shrink pane to the right by holding Alt+Shift+Right."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        send_keys("%+{RIGHT}")
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_up(win):
    """Shrink/grow pane upward by holding Alt+Shift+Up."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        send_keys("%+{UP}")
        time.sleep(random.uniform(0.02, 0.08))


def resize_pane_down(win):
    """Grow/shrink pane downward by holding Alt+Shift+Down."""
    _ensure_focused(win)
    repeats = _get_resize_repeats()
    for _ in range(repeats):
        send_keys("%+{DOWN}")
        time.sleep(random.uniform(0.02, 0.08))


def focus_pane_left(win):
    """Move focus to the left pane (Alt+Left)."""
    _ensure_focused(win)
    send_keys("%{LEFT}")
    _brief_sleep(50)


def focus_pane_right(win):
    """Move focus to the right pane (Alt+Right)."""
    _ensure_focused(win)
    send_keys("%{RIGHT}")
    _brief_sleep(50)


def focus_pane_up(win):
    """Move focus to the pane above (Alt+Up)."""
    _ensure_focused(win)
    send_keys("%{UP}")
    _brief_sleep(50)


def focus_pane_down(win):
    """Move focus to the pane below (Alt+Down)."""
    _ensure_focused(win)
    send_keys("%{DOWN}")
    _brief_sleep(50)


def new_tab(win):
    """Open a new tab (Ctrl+Shift+T)."""
    _ensure_focused(win)
    send_keys("^+t")
    _brief_sleep(300)


def close_tab(win):
    """Close the current tab, but open a new one first to prevent closing WT."""
    _ensure_focused(win)
    send_keys("^+t")
    _brief_sleep(200)
    send_keys("^+{TAB}")
    _brief_sleep(100)
    send_keys("^+w")
    _brief_sleep(150)


def next_tab(win):
    """Switch to the next tab (Ctrl+Tab)."""
    _ensure_focused(win)
    send_keys("^{TAB}")
    _brief_sleep(80)


def prev_tab(win):
    """Switch to the previous tab (Ctrl+Shift+Tab)."""
    _ensure_focused(win)
    send_keys("^+{TAB}")
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
        send_keys(safe, pause=0.01)
    except Exception:
        pass
    _brief_sleep(50)


def type_enter(win):
    """Press Enter."""
    _ensure_focused(win)
    send_keys("{ENTER}")
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
    send_keys(cmd + "{ENTER}", pause=0.02)
    _brief_sleep(100)


def scroll_up(win):
    """Scroll up in the terminal (Ctrl+Shift+Up or mouse wheel)."""
    _ensure_focused(win)
    repeats = random.randint(1, 20)
    for _ in range(repeats):
        send_keys("^+{UP}")
        time.sleep(0.02)


def scroll_down(win):
    """Scroll down in the terminal (Ctrl+Shift+Down or mouse wheel)."""
    _ensure_focused(win)
    repeats = random.randint(1, 20)
    for _ in range(repeats):
        send_keys("^+{DOWN}")
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
    send_keys("{F11}")
    _brief_sleep(300)


def zoom_in(win):
    """Increase font size (Ctrl+=)."""
    _ensure_focused(win)
    repeats = random.randint(1, 5)
    for _ in range(repeats):
        send_keys("^{=}")
        time.sleep(0.05)


def zoom_out(win):
    """Decrease font size (Ctrl+-)."""
    _ensure_focused(win)
    repeats = random.randint(1, 5)
    for _ in range(repeats):
        send_keys("^{-}")
        time.sleep(0.05)


def zoom_reset(win):
    """Reset font size (Ctrl+0)."""
    _ensure_focused(win)
    send_keys("^0")
    _brief_sleep(50)


def open_search(win):
    """Open the search dialog (Ctrl+Shift+F)."""
    _ensure_focused(win)
    send_keys("^+f")
    _brief_sleep(200)
    text = "".join(random.choices(string.ascii_letters, k=random.randint(1, 20)))
    send_keys(text, pause=0.02)
    _brief_sleep(100)
    send_keys("{ESC}")
    _brief_sleep(50)


def mouse_click_random(win):
    """Click at a random position within the terminal window."""
    _ensure_focused(win)
    try:
        rect = win.rectangle()
        x = random.randint(rect.left + 10, max(rect.left + 11, rect.right - 10))
        y = random.randint(rect.top + 10, max(rect.top + 11, rect.bottom - 10))
        mouse.click(coords=(x, y))
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
        mouse.press(coords=(x1, y1))
        time.sleep(0.02)
        mouse.move(coords=(x2, y2))
        time.sleep(0.02)
        mouse.release(coords=(x2, y2))
    except Exception as e:
        logger.warning(f"mouse_drag_random failed: {e}")
    _brief_sleep(50)


def copy_paste(win):
    """Copy selection then paste (Ctrl+Shift+C, Ctrl+Shift+V)."""
    _ensure_focused(win)
    send_keys("^+c")
    _brief_sleep(50)
    send_keys("^+v")
    _brief_sleep(50)


def open_settings(win):
    """Open settings and immediately close (Ctrl+,)."""
    _ensure_focused(win)
    send_keys("^{,}")
    _brief_sleep(500)
    send_keys("^+w")
    _brief_sleep(150)


def open_command_palette(win):
    """Open command palette (Ctrl+Shift+P) and dismiss."""
    _ensure_focused(win)
    send_keys("^+p")
    _brief_sleep(300)
    send_keys("{ESC}")
    _brief_sleep(100)


def command_palette_random(win):
    """
    Open command palette and pick a random item by arrowing down
    a random number of times and pressing Enter.
    """
    _ensure_focused(win)
    send_keys("^+p")
    _brief_sleep(300)
    steps = random.randint(1, 30)
    for _ in range(steps):
        send_keys("{DOWN}")
        time.sleep(0.02)
    send_keys("{ENTER}")
    logger.info(f"Command palette: selected item at position ~{steps}")
    _brief_sleep(200)


def command_palette_search(win):
    """
    Open command palette, type a partial search term to filter commands,
    then select one of the filtered results.
    """
    _ensure_focused(win)
    terms = [
        "split", "close", "tab", "pane", "font", "size", "color",
        "theme", "scroll", "mark", "copy", "paste", "find", "reset",
        "zoom", "move", "focus", "toggle", "full", "new", "rename",
        "duplicate", "switch", "select", "clear", "export",
    ]
    term = random.choice(terms)
    send_keys("^+p")
    _brief_sleep(200)
    send_keys(term, pause=0.02)
    _brief_sleep(150)
    steps = random.randint(0, 5)
    for _ in range(steps):
        send_keys("{DOWN}")
        time.sleep(0.02)
    send_keys("{ENTER}")
    logger.info(f"Command palette: searched '{term}', selected position ~{steps}")
    _brief_sleep(200)


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
    send_keys(cmd + "{ENTER}", pause=0.02)
    logger.info(f"Launched TerminalStress via: {cmd}")
    _brief_sleep(500)


def stop_terminal_stress(win):
    """Send Ctrl+C to the focused pane to stop a running TerminalStress."""
    _ensure_focused(win)
    send_keys("^c")
    _brief_sleep(200)


# Build the action catalog with weights.
# Higher weight = more likely to be selected.
# Pane resize actions have the highest weight since that's where bugs tend to hide.
def build_action_catalog() -> list[Action]:
    global _mitigations
    _mitigations = load_known_bugs()

    return [
        # Pane resize (high weight — this is where bugs like #7416 and #19996 live)
        Action("resize_pane_left", 15, resize_pane_left),
        Action("resize_pane_right", 15, resize_pane_right),
        Action("resize_pane_up", 10, resize_pane_up),
        Action("resize_pane_down", 10, resize_pane_down),

        # Pane management
        Action("split_pane_right", 12, split_pane_right),
        Action("split_pane_down", 12, split_pane_down),
        Action("close_pane", 6, close_pane),

        # Pane focus
        Action("focus_pane_left", 8, focus_pane_left),
        Action("focus_pane_right", 8, focus_pane_right),
        Action("focus_pane_up", 4, focus_pane_up),
        Action("focus_pane_down", 4, focus_pane_down),

        # Tab management
        Action("new_tab", 3, new_tab),
        Action("close_tab", 2, close_tab),
        Action("next_tab", 3, next_tab),
        Action("prev_tab", 3, prev_tab),

        # Typing
        Action("type_random_text", 5, type_random_text),
        Action("type_enter", 5, type_enter),
        Action("type_command", 4, type_command),

        # Scrolling
        Action("scroll_up", 4, scroll_up),
        Action("scroll_down", 4, scroll_down),

        # Window management
        Action("resize_window", 6, resize_window),
        Action("maximize_window", 2, maximize_window),
        Action("minimize_restore_window", 1, minimize_restore_window),

        # Zoom
        Action("zoom_in", 2, zoom_in),
        Action("zoom_out", 2, zoom_out),
        Action("zoom_reset", 2, zoom_reset),

        # Search & UI
        Action("open_search", 2, open_search),
        Action("open_command_palette", 1, open_command_palette),
        Action("command_palette_random", 3, command_palette_random),
        Action("command_palette_search", 3, command_palette_search),
        Action("open_settings", 1, open_settings),

        # Mouse
        Action("mouse_click_random", 4, mouse_click_random),
        Action("mouse_drag_random", 2, mouse_drag_random),

        # Clipboard
        Action("copy_paste", 2, copy_paste),

        # TerminalStress
        Action("run_terminal_stress", 2, run_terminal_stress),
        Action("stop_terminal_stress", 1, stop_terminal_stress),
    ]


def pick_action(catalog: list[Action]) -> Action:
    """Select a random action from the catalog using weighted random selection."""
    names = [a.name for a in catalog]
    weights = [a.weight for a in catalog]
    chosen = random.choices(catalog, weights=weights, k=1)[0]
    return chosen
