"""Keep the computer from sleeping while trips are being watched (a sleeping PC watches nothing). Idle sleep only:
closing a laptop's lid still follows the system's lid setting.
Windows: SetThreadExecutionState (call from a long-lived thread: the request applies to the calling thread).
macOS: `caffeinate`. Linux: `systemd-inhibit` (without systemd it stays with the desktop's power settings)."""
from __future__ import annotations

import os
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_blocker = None


def _command() -> list:
    """Stays awake only as long as this process lives, even if the bot crashes."""
    pid = str(os.getpid())
    if sys.platform == "darwin":
        return ["caffeinate", "-i", "-w", pid]
    return ["systemd-inhibit", "--what=idle:sleep", "--who=UzRail Bot", "--why=Watching for train tickets",
            "tail", f"--pid={pid}", "-f", "/dev/null"]


def keep_awake(on: bool):
    global _blocker
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0))
        return
    running = _blocker is not None and _blocker.poll() is None
    if on and not running:
        try:
            _blocker = subprocess.Popen(_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            _blocker = None
    elif not on and running:
        _blocker.terminate()
        _blocker = None
