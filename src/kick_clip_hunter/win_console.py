"""Stops a click in the server's console window from freezing the app.

Windows consoles have QuickEdit mode on by default: clicking anywhere in the
window starts a text selection, and while one is active the console blocks
every write to it until Enter/Esc is pressed. The app logs from the event
loop, so one stray click silently halts everything - webhook handling,
detection, recording ticks - until someone notices and presses a key.
"""

import atexit
import sys

_STD_INPUT_HANDLE = -10
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080


def disable_quick_edit() -> bool:
    """Turn QuickEdit off for this process's console, if it has one.

    Returns whether anything was changed. The console outlives the process,
    so the original mode is put back on a normal interpreter exit.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    mode = ctypes.c_uint32()
    # Fails when stdin isn't a console at all (redirected, run as a service).
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    if not mode.value & _ENABLE_QUICK_EDIT_MODE:
        return False
    original = mode.value
    # ENABLE_EXTENDED_FLAGS has to be set for a QuickEdit change to apply.
    if not kernel32.SetConsoleMode(handle, (original & ~_ENABLE_QUICK_EDIT_MODE) | _ENABLE_EXTENDED_FLAGS):
        return False
    atexit.register(kernel32.SetConsoleMode, handle, original)
    return True
