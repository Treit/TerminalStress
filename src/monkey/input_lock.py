"""
Cross-process input serialization for monkey testing.

Uses a Windows named mutex to ensure only one monkey instance
calls SendInput at a time, preventing modifier-key interleaving
that triggers Start Menu / Alt+Tab activation.
"""

import ctypes
import ctypes.wintypes
import logging

logger = logging.getLogger("monkey")

kernel32 = ctypes.windll.kernel32

# Win32 constants
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

MUTEX_NAME = r"Global\MonkeyInputLock"

# Configure ctypes function signatures for type safety.
kernel32.CreateMutexW.argtypes = [
    ctypes.wintypes.LPVOID,  # lpMutexAttributes
    ctypes.wintypes.BOOL,    # bInitialOwner
    ctypes.wintypes.LPCWSTR, # lpName
]
kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE

kernel32.WaitForSingleObject.argtypes = [
    ctypes.wintypes.HANDLE,  # hHandle
    ctypes.wintypes.DWORD,   # dwMilliseconds
]
kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD

kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
kernel32.ReleaseMutex.restype = ctypes.wintypes.BOOL

kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
kernel32.CloseHandle.restype = ctypes.wintypes.BOOL


class InputLock:
    """A cross-process lock backed by a Windows named mutex.

    Designed to serialize SendInput calls across multiple monkey
    processes so that modifier-key events (ALT, CTRL, etc.) from
    different processes do not interleave and accidentally trigger
    the Start Menu or Alt+Tab.

    Usage::

        lock = InputLock()
        with lock:
            # Only one process executes this block at a time.
            send_input(...)

    If the mutex cannot be created (e.g., insufficient privileges),
    the lock degrades to a no-op and logs a warning.
    """

    def __init__(self, timeout_ms: int = 2000) -> None:
        """Create or open the named mutex.

        Args:
            timeout_ms: Maximum time in milliseconds to wait when
                acquiring the lock.  Defaults to 2000 ms.
        """
        self._timeout_ms = timeout_ms
        self._handle: ctypes.wintypes.HANDLE | None = None
        self._owned = False

        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            logger.warning(
                "CreateMutexW failed (error %d); input lock disabled",
                ctypes.get_last_error(),
            )
            return

        self._handle = handle
        logger.debug("Opened named mutex %r", MUTEX_NAME)

    # -- context manager protocol ------------------------------------------

    def __enter__(self) -> "InputLock":
        """Acquire the mutex, blocking up to *timeout_ms*.

        * ``WAIT_OBJECT_0``  – mutex acquired normally.
        * ``WAIT_ABANDONED`` – previous owner crashed; treated as acquired.
        * ``WAIT_TIMEOUT``   – timed out; log a warning and proceed
          (best-effort locking).
        * ``WAIT_FAILED``    – unexpected error; log and proceed.

        Returns:
            *self*, so the lock can be used in a ``with`` statement.
        """
        if self._handle is None:
            return self

        result = kernel32.WaitForSingleObject(
            self._handle, ctypes.wintypes.DWORD(self._timeout_ms)
        )

        if result == WAIT_OBJECT_0:
            self._owned = True
        elif result == WAIT_ABANDONED:
            logger.warning(
                "Input mutex was abandoned by a crashed process; "
                "acquiring anyway"
            )
            self._owned = True
        elif result == WAIT_TIMEOUT:
            logger.warning(
                "Timed out waiting for input mutex after %d ms; "
                "proceeding without lock",
                self._timeout_ms,
            )
            self._owned = False
        else:
            logger.warning(
                "WaitForSingleObject returned unexpected value 0x%08X; "
                "proceeding without lock",
                result,
            )
            self._owned = False

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        """Release the mutex if it was successfully acquired."""
        if self._handle is not None and self._owned:
            kernel32.ReleaseMutex(self._handle)
            self._owned = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying mutex handle.

        Safe to call multiple times.  After this call the lock
        becomes a no-op.
        """
        if self._handle is not None:
            kernel32.CloseHandle(self._handle)
            self._handle = None
            self._owned = False
            logger.debug("Closed named mutex %r", MUTEX_NAME)


# -- module-level singleton ------------------------------------------------

_lock: InputLock | None = None


def get_input_lock() -> InputLock:
    """Return (and lazily create) the module-level :class:`InputLock` singleton."""
    global _lock  # noqa: PLW0603
    if _lock is None:
        _lock = InputLock()
    return _lock
