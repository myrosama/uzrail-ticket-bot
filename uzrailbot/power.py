"""Keep the computer from sleeping while trips are being watched (a sleeping PC watches nothing).
Windows only; call from a long-lived thread (the request applies to the calling thread)."""
from __future__ import annotations

import os

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool):
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0))
