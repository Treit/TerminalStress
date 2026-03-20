"""
Low-level keyboard hook that suppresses dangerous system hotkeys during monkey testing.

Without this guard, stray ALT key events open the Start Menu and ALT+TAB switches
apps, causing the terminal under test to lose focus permanently.

Usage::

    with InputGuard() as guard:
        # ALT→Start Menu, WIN key, ALT+TAB, ALT+ESC are all suppressed.
        # Ctrl+C, normal typing, and intentional ALT+key WT shortcuts pass through.
        run_monkey_test()
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
import time

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

HC_ACTION = 0

VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5

# Modifier virtual-key codes that should NOT count as "a key was pressed
# between ALT-down and ALT-up" for the bare-ALT → Start Menu suppression.
_MODIFIER_VKS = frozenset({
    0x10,  # VK_SHIFT
    0x11,  # VK_CONTROL
    VK_MENU,
    0xA0,  # VK_LSHIFT
    0xA1,  # VK_RSHIFT
    0xA2,  # VK_LCONTROL
    0xA3,  # VK_RCONTROL
    VK_LMENU,
    VK_RMENU,
})

_ALT_VKS = frozenset({VK_MENU, VK_LMENU, VK_RMENU})
_WIN_VKS = frozenset({VK_LWIN, VK_RWIN})

# ---------------------------------------------------------------------------
# Win32 structures & function prototypes
# ---------------------------------------------------------------------------
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,          # return: LRESULT
    ctypes.c_int,           # nCode
    ctypes.wintypes.WPARAM, # wParam  (message id)
    ctypes.wintypes.LPARAM, # lParam  (pointer to KBDLLHOOKSTRUCT)
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """Mirror of the Win32 KBDLLHOOKSTRUCT."""

    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_SetWindowsHookExW = _user32.SetWindowsHookExW
_SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    HOOKPROC,
    ctypes.wintypes.HINSTANCE,
    ctypes.wintypes.DWORD,
]
_SetWindowsHookExW.restype = ctypes.wintypes.HHOOK

_UnhookWindowsHookEx = _user32.UnhookWindowsHookEx
_UnhookWindowsHookEx.argtypes = [ctypes.wintypes.HHOOK]
_UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL

_CallNextHookEx = _user32.CallNextHookEx
_CallNextHookEx.argtypes = [
    ctypes.wintypes.HHOOK,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
_CallNextHookEx.restype = ctypes.c_long

_GetMessageW = _user32.GetMessageW
_GetMessageW.argtypes = [
    ctypes.POINTER(ctypes.wintypes.MSG),
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.UINT,
]
_GetMessageW.restype = ctypes.wintypes.BOOL

_TranslateMessage = _user32.TranslateMessage
_TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
_TranslateMessage.restype = ctypes.wintypes.BOOL

_DispatchMessageW = _user32.DispatchMessageW
_DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
_DispatchMessageW.restype = ctypes.c_long

_PostThreadMessageW = _user32.PostThreadMessageW
_PostThreadMessageW.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
_PostThreadMessageW.restype = ctypes.wintypes.BOOL

_GetCurrentThreadId = _kernel32.GetCurrentThreadId
_GetCurrentThreadId.argtypes = []
_GetCurrentThreadId.restype = ctypes.wintypes.DWORD

log = logging.getLogger("monkey")

# ---------------------------------------------------------------------------
# InputGuard
# ---------------------------------------------------------------------------


class InputGuard:
    """Installs a ``WH_KEYBOARD_LL`` hook to suppress dangerous system hotkeys.

    Suppressed events
    -----------------
    * **Bare ALT release → Start Menu** – ALT key-up with no non-modifier key
      pressed since the matching ALT key-down.
    * **WIN key → Start Menu** – both key-down and key-up of ``VK_LWIN`` /
      ``VK_RWIN``.
    * **ALT+TAB → App switcher** – TAB while ALT is held.
    * **ALT+ESC → Window cycling** – ESC while ALT is held.

    Events that are **never** suppressed
    -------------------------------------
    * Ctrl+C (so the monkey process can be interrupted).
    * Normal keystrokes when ALT is not involved.
    * Intentional ALT+<key> terminal shortcuts (e.g. ALT+SHIFT+Arrow).
    """

    def __init__(self) -> None:
        self._hook: ctypes.wintypes.HHOOK | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

        # Bare-ALT tracking: True when a non-modifier key has been pressed
        # between the most recent ALT-down and the current moment.
        self._alt_combo_key_pressed = False

        # Keep a strong reference so the GC cannot collect the callback while
        # the hook is installed.
        self._hook_proc = HOOKPROC(self._low_level_keyboard_proc)

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "InputGuard":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        self.stop()

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Install the keyboard hook and start the message-pump thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("InputGuard is already running")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._alt_combo_key_pressed = False

        self._thread = threading.Thread(
            target=self._run, name="InputGuard", daemon=True
        )
        self._thread.start()

        # Wait until the hook is actually installed before returning so the
        # caller can rely on the guard being active immediately.
        self._ready_event.wait(timeout=5.0)
        if not self._ready_event.is_set():
            log.error("InputGuard thread did not become ready in time")

    def stop(self) -> None:
        """Unhook and stop the message-pump thread."""
        self._stop_event.set()
        if self._thread_id is not None:
            _PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning("InputGuard thread did not exit cleanly")
        self._thread = None
        self._thread_id = None

    # -- internal -----------------------------------------------------------

    def _run(self) -> None:
        """Thread entry point: install hook, pump messages, clean up."""
        self._thread_id = _GetCurrentThreadId()

        self._hook = _SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._hook_proc,
            None,  # hInstance – NULL for global LL hooks
            0,     # dwThreadId – 0 means all threads
        )

        if not self._hook:
            log.error("SetWindowsHookExW failed – keyboard guard inactive")
            self._ready_event.set()
            return

        log.info("Keyboard guard installed (hook=%s)", self._hook)
        self._ready_event.set()

        # Message pump — required for WH_KEYBOARD_LL to work.
        msg = ctypes.wintypes.MSG()
        while not self._stop_event.is_set():
            result = _GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                # 0  → WM_QUIT received
                # -1 → error
                break
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            _UnhookWindowsHookEx(self._hook)
            log.info("Keyboard guard removed")
            self._hook = None

    def _low_level_keyboard_proc(
        self,
        nCode: int,
        wParam: int,
        lParam: int,
    ) -> int:
        """``WH_KEYBOARD_LL`` callback.

        Returns 1 to suppress an event, or delegates to ``CallNextHookEx``
        to let it through.

        NOTE: No logging in this callback — file I/O could exceed Windows'
        ~300ms hook timeout, causing missed keystrokes.
        """
        if not self._hook:
            return 0

        if nCode == HC_ACTION:
            kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kbd.vkCode

            is_key_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_key_up = wParam in (WM_KEYUP, WM_SYSKEYUP)

            # ------ WIN key: suppress entirely ----------------------------
            if vk in _WIN_VKS:
                return 1

            # ------ ALT tracking for bare-ALT suppression -----------------
            if vk in _ALT_VKS:
                if is_key_down:
                    self._alt_combo_key_pressed = False
                elif is_key_up:
                    if not self._alt_combo_key_pressed:
                        return 1

            # ------ Track non-modifier keys while ALT is held -------------
            if is_key_down and vk not in _MODIFIER_VKS and vk not in _WIN_VKS:
                self._alt_combo_key_pressed = True

            # ------ ALT+TAB: suppress TAB ---------------------------------
            if vk == VK_TAB and wParam == WM_SYSKEYDOWN:
                return 1

            # ------ ALT+ESC: suppress ESC ---------------------------------
            if vk == VK_ESCAPE and wParam == WM_SYSKEYDOWN:
                return 1

        return _CallNextHookEx(self._hook, nCode, wParam, lParam)


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("InputGuard active for 10 seconds.  Try ALT, WIN, ALT+TAB …")
    print("Press Ctrl+C to stop early.")

    with InputGuard():
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            pass

    print("InputGuard stopped.")
