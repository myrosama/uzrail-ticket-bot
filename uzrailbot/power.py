"""Keep the computer from sleeping while trips are being watched (a sleeping PC watches nothing).
Windows: SetThreadExecutionState (call from a long-lived thread: the request applies to the calling thread).
macOS: a `caffeinate -i` process tied to ours. Linux: left to the desktop's power settings."""
from __future__ import annotations

import os
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_caffeinate = None


def keep_awake(on: bool):
    global _caffeinate
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0))
    elif sys.platform == "darwin":
        running = _caffeinate is not None and _caffeinate.poll() is None
        if on and not running:
            try:      # -i: no idle sleep; -w: ends by itself when this process ends
                _caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                _caffeinate = None
        elif not on and running:
            _caffeinate.terminate()
            _caffeinate = None
